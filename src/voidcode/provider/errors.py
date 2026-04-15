from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from .protocol import ProviderExecutionError

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


@dataclass(frozen=True, slots=True)
class ParsedProviderError:
    kind: Literal["rate_limit", "context_limit", "invalid_model", "transient_failure"]
    message: str
    details: dict[str, object]


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
) -> Literal["rate_limit", "context_limit", "invalid_model", "transient_failure"]:
    if _is_context_overflow(message, status_code, code):
        return "context_limit"

    if status_code == 429 or (
        isinstance(code, str) and code.lower() in {"rate_limit", "rate_limit_exceeded"}
    ):
        return "rate_limit"

    if status_code in {401, 403, 404}:
        return "invalid_model"

    if isinstance(code, str) and code.lower() in {
        "invalid_model",
        "model_not_found",
        "invalid_api_key",
        "insufficient_quota",
        "usage_not_included",
        "authentication_error",
    }:
        return "invalid_model"

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
    details: dict[str, object] = dict(payload)
    details.update(
        {
            "source": "api",
            "status_code": status_code,
            "error_code": code,
        }
    )
    return ParsedProviderError(kind=kind, message=message, details=details)


def parse_provider_stream_error(payload: dict[str, Any]) -> ParsedProviderError:
    message = _extract_error_message(payload) or "provider stream error"
    status_code = _extract_status_code(payload)
    code = _extract_error_code(payload)
    kind = _classify_api_error_kind(message=message, status_code=status_code, code=code)
    details: dict[str, object] = dict(payload)
    details.update(
        {
            "source": "stream",
            "status_code": status_code,
            "error_code": code,
        }
    )
    return ParsedProviderError(kind=kind, message=message, details=details)


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
