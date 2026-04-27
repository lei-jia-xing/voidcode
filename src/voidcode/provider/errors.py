from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from .protocol import ProviderErrorKind, ProviderExecutionError

_CONTEXT_OVERFLOW_PATTERNS = (
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"input is too long for requested model", re.IGNORECASE),
    re.compile(r"exceeds the context window", re.IGNORECASE),
    re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),
    re.compile(r"maximum prompt length is \d+", re.IGNORECASE),
    re.compile(r"reduce the length of the messages", re.IGNORECASE),
    re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),
    re.compile(r"exceeds the limit of \d+", re.IGNORECASE),
    re.compile(r"exceeds the available context size", re.IGNORECASE),
    re.compile(r"greater than the context length", re.IGNORECASE),
    re.compile(r"context window exceeds limit", re.IGNORECASE),
    re.compile(r"exceeded model token limit", re.IGNORECASE),
    re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),
    re.compile(r"request entity too large", re.IGNORECASE),
    re.compile(r"context length is only \d+ tokens", re.IGNORECASE),
    re.compile(r"input length.*exceeds.*context length", re.IGNORECASE),
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.IGNORECASE),
    re.compile(r"too large for model with \d+ maximum context length", re.IGNORECASE),
    re.compile(r"model_context_window_exceeded", re.IGNORECASE),
    re.compile(r"context window", re.IGNORECASE),
    re.compile(r"context limit", re.IGNORECASE),
    re.compile(r"maximum context", re.IGNORECASE),
    re.compile(r"maximum context length", re.IGNORECASE),
    re.compile(r"token limit", re.IGNORECASE),
)

_INVALID_MODEL_PATTERNS = (
    re.compile(r"model .* not found", re.IGNORECASE),
    re.compile(r"unknown model", re.IGNORECASE),
    re.compile(r"invalid model", re.IGNORECASE),
    re.compile(r"model_not_found", re.IGNORECASE),
)

_SENSITIVE_DETAIL_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "key",
    "password",
    "secret",
    "token",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{6,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)"),
)


def _redact_secret_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}=<redacted>"
                if match.lastindex and match.lastindex >= 2
                else "<redacted>"
            ),
            redacted,
        )
    return redacted


def _redact_provider_error_detail(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for raw_key, raw_item in cast(dict[object, object], value).items():
            key = str(raw_key)
            lowered = key.lower()
            if any(marker in lowered for marker in _SENSITIVE_DETAIL_KEY_MARKERS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_provider_error_detail(raw_item)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_error_detail(item) for item in cast(list[object], value)]
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value


def _provider_error_details(payload: dict[str, Any]) -> dict[str, object]:
    return cast(dict[str, object], _redact_provider_error_detail(payload))


@dataclass(frozen=True, slots=True)
class ParsedProviderError:
    kind: ProviderErrorKind
    message: str
    details: dict[str, object]
    retryable: bool
    fallback_allowed: bool
    guidance: str


def _recovery_policy_for_kind(kind: ProviderErrorKind) -> tuple[bool, bool]:
    if kind == "context_limit":
        return False, False
    if kind in {
        "missing_auth",
        "invalid_model",
        "unsupported_feature",
        "stream_tool_feedback_shape",
    }:
        return False, True
    if kind == "rate_limit":
        return True, True
    if kind == "cancelled":
        return False, False
    return True, True


def guidance_for_provider_error_kind(kind: ProviderErrorKind) -> str:
    if kind == "missing_auth":
        return "Configure the provider API key or auth method, then retry."
    if kind == "invalid_model":
        return "Check the configured provider/model name and model access permissions."
    if kind == "rate_limit":
        return "Retry later, reduce request volume, or configure a fallback model."
    if kind == "context_limit":
        return (
            "Reduce prompt/tool-result context or switch to a model with a larger context window."
        )
    if kind == "unsupported_feature":
        return (
            "Disable the unsupported provider feature or choose a model/provider that supports it."
        )
    if kind == "stream_tool_feedback_shape":
        return (
            "Report this provider stream/tool-call shape; VoidCode could not normalize it safely."
        )
    if kind == "cancelled":
        return "The request was cancelled; rerun when ready."
    return "Retry the request or configure a fallback provider/model."


def _extract_error_message(payload: dict[str, Any]) -> str | None:
    direct = payload.get("message")
    if isinstance(direct, str) and direct.strip():
        return direct

    error_obj = payload.get("error")
    if isinstance(error_obj, str) and error_obj.strip():
        return error_obj
    if isinstance(error_obj, dict):
        error_payload = dict(cast(dict[str, Any], error_obj))
        nested_message = error_payload.get("message")
        if isinstance(nested_message, str) and nested_message.strip():
            return nested_message
    return None


def _extract_error_code(payload: dict[str, Any]) -> str | None:
    code = payload.get("code")
    if isinstance(code, str) and code.strip():
        return code
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        error_payload = dict(cast(dict[str, Any], error_obj))
        nested_code = error_payload.get("code")
        if isinstance(nested_code, str) and nested_code.strip():
            return nested_code
    return None


def _extract_status_code(payload: dict[str, Any]) -> int | None:
    raw_status = payload.get("status_code")
    if isinstance(raw_status, int):
        return raw_status
    if isinstance(raw_status, str) and raw_status.isdigit():
        return int(raw_status)
    return None


def _is_context_overflow(message: str, status_code: int | None, code: str | None) -> bool:
    if status_code == 413:
        return True
    if isinstance(code, str) and code.lower() == "context_length_exceeded":
        return True
    return any(pattern.search(message) is not None for pattern in _CONTEXT_OVERFLOW_PATTERNS)


def _classify_api_error_kind(
    *,
    message: str,
    status_code: int | None,
    code: str | None,
) -> ProviderErrorKind:
    if _is_context_overflow(message, status_code, code):
        return "context_limit"

    if status_code == 429 or (
        isinstance(code, str) and code.lower() in {"rate_limit", "rate_limit_exceeded"}
    ):
        return "rate_limit"

    if isinstance(code, str) and code.lower() in {
        "missing_api_key",
        "invalid_api_key",
        "authentication_error",
        "unauthorized",
    }:
        return "missing_auth"

    if isinstance(code, str) and code.lower() in {
        "invalid_model",
        "model_not_found",
        "insufficient_quota",
        "usage_not_included",
    }:
        return "invalid_model"

    if status_code == 401:
        return "missing_auth"

    if status_code == 404:
        return "invalid_model"

    lowered_message = message.lower()
    if any(
        marker in lowered_message
        for marker in (
            "api key is missing",
            "missing api key",
            "invalid api key",
            "authentication failed",
            "unauthorized",
        )
    ):
        return "missing_auth"

    if status_code == 403:
        if any(pattern.search(message) is not None for pattern in _INVALID_MODEL_PATTERNS):
            return "invalid_model"
        if any(
            marker in lowered_message
            for marker in (
                "not authorized for model",
                "model access",
                "model unavailable",
                "permission to access model",
                "usage not included",
            )
        ):
            return "invalid_model"
        return "missing_auth"

    if any(
        marker in lowered_message
        for marker in (
            "unsupported stream",
            "streaming is not supported",
            "tools are not supported",
            "tool calling is not supported",
        )
    ):
        return "unsupported_feature"

    if any(
        marker in lowered_message
        for marker in (
            "invalid tool call",
            "tool_calls must",
            "malformed tool call",
            "stream tool",
        )
    ):
        return "stream_tool_feedback_shape"

    if any(pattern.search(message) is not None for pattern in _INVALID_MODEL_PATTERNS):
        return "invalid_model"

    if status_code is not None and status_code >= 500:
        return "transient_failure"

    return "transient_failure"


def parse_provider_api_error(payload: dict[str, Any]) -> ParsedProviderError:
    message = _extract_error_message(payload) or "provider api error"
    status_code = _extract_status_code(payload)
    code = _extract_error_code(payload)
    kind = _classify_api_error_kind(message=message, status_code=status_code, code=code)
    retryable, fallback_allowed = _recovery_policy_for_kind(kind)
    details = _provider_error_details(payload)
    guidance = guidance_for_provider_error_kind(kind)
    details.update(
        {
            "source": "api",
            "status_code": status_code,
            "error_code": code,
            "guidance": guidance,
        }
    )
    return ParsedProviderError(
        kind=kind,
        message=message,
        details=details,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        guidance=guidance,
    )


def parse_provider_stream_error(payload: dict[str, Any]) -> ParsedProviderError:
    message = _extract_error_message(payload) or "provider stream error"
    status_code = _extract_status_code(payload)
    code = _extract_error_code(payload)
    kind = _classify_api_error_kind(message=message, status_code=status_code, code=code)
    retryable, fallback_allowed = _recovery_policy_for_kind(kind)
    details = _provider_error_details(payload)
    guidance = guidance_for_provider_error_kind(kind)
    details.update(
        {
            "source": "stream",
            "status_code": status_code,
            "error_code": code,
            "guidance": guidance,
        }
    )
    return ParsedProviderError(
        kind=kind,
        message=message,
        details=details,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        guidance=guidance,
    )


def provider_execution_error_from_api_payload(
    *,
    provider_name: str,
    model_name: str,
    payload: dict[str, Any],
) -> ProviderExecutionError:
    parsed = parse_provider_api_error(payload)
    return ProviderExecutionError(
        kind=parsed.kind,
        provider_name=provider_name,
        model_name=model_name,
        message=parsed.message,
        retryable=parsed.retryable,
        details=parsed.details,
    )


def provider_execution_error_from_stream_payload(
    *,
    provider_name: str,
    model_name: str,
    payload: dict[str, Any],
) -> ProviderExecutionError:
    parsed = parse_provider_stream_error(payload)
    return ProviderExecutionError(
        kind=parsed.kind,
        provider_name=provider_name,
        model_name=model_name,
        message=parsed.message,
        retryable=parsed.retryable,
        details=parsed.details,
    )


class SingleAgentProviderError(ValueError):
    """Base runtime-classified provider error."""


class SingleAgentContextLimitError(SingleAgentProviderError):
    """Provider failure caused by context window exhaustion."""


def format_invalid_provider_config_error(field_path: str, reason: str) -> str:
    return f"invalid provider config: {field_path} {reason}"


def format_fallback_exhausted_error(*, provider_name: str, model_name: str, attempt: int) -> str:
    return (
        "provider fallback exhausted after "
        f"{provider_name}/{model_name} failed at attempt {attempt}"
    )


def classify_provider_error(exc: Exception) -> SingleAgentProviderError | None:
    message = str(exc).lower()
    context_limit_markers = (
        "context window",
        "context limit",
        "maximum context",
        "maximum context length",
        "token limit",
    )
    if any(marker in message for marker in context_limit_markers):
        return SingleAgentContextLimitError(str(exc))
    return None
