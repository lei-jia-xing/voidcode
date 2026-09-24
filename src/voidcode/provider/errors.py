from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, overload

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


# Class-path wrapper emitted by higher-level SDK shims, e.g. `openai.AuthenticationError: ...`.
_CLASS_PATH_WRAPPER_PREFIX = re.compile(
    r"^(?:openai|anthropic)(?:\.[A-Za-z0-9_]+)*(?:Error)?\s*:\s*",
    re.IGNORECASE,
)
# Wrapper the official SDKs build for non-2xx responses: `Error code: <status> - <body>`
# (see openai/anthropic `_base_client.py`). The body is the parsed response payload.
_SDK_ERROR_WRAPPER_PREFIX = re.compile(r"^Error code:\s*\d+", re.IGNORECASE)


def _message_from_container(value: object) -> str | None:
    """Provider message text nested in a decoded error payload (`message` or `error.message`)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        mapping = value
        direct = mapping.get("message")
        if isinstance(direct, str) and direct.strip():
            return direct
        return _message_from_container(mapping.get("error"))
    return None


def _sdk_error_wrapper_body(value: str) -> str | None:
    """Provider text inside an SDK `Error code: <status> - <body>` wrapper, without the wrapper."""
    text = value.strip()
    match = _SDK_ERROR_WRAPPER_PREFIX.match(text)
    if match is None:
        return None
    remainder = text[match.end() :].strip()
    if not remainder.startswith("-"):
        return None
    body = remainder[1:].strip()
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except ValueError:
        return body
    return _message_from_container(decoded) or body


def _nested_error_message(payload: dict[str, Any]) -> str | None:
    return _message_from_container(payload.get("error"))


def _strip_provider_error_wrapper(value: str) -> str:
    stripped = _CLASS_PATH_WRAPPER_PREFIX.sub("", value.strip())
    unwrapped = _sdk_error_wrapper_body(stripped)
    return stripped if unwrapped is None else unwrapped


def _redact_secret_text(value: str) -> str:
    redacted = _strip_provider_error_wrapper(value)
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(
            lambda match: f"{match.group(1)}=<redacted>" if match.lastindex and match.lastindex >= 2 else "<redacted>",
            redacted,
        )
    return redacted


def _redact_provider_error_detail(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(marker in lowered for marker in _SENSITIVE_DETAIL_KEY_MARKERS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_provider_error_detail(raw_item)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_error_detail(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value


def redact_provider_error_message(value: str) -> str:
    """Return provider-facing error text with credential-like values masked."""
    return _redact_secret_text(value)


@overload
def redact_provider_error_details(value: Mapping[str, object]) -> dict[str, object]: ...


@overload
def redact_provider_error_details(value: object) -> object: ...


def redact_provider_error_details(value: object) -> object:
    """Recursively redact provider diagnostics before runtime persistence."""
    return _redact_provider_error_detail(value)


def _provider_error_details(payload: dict[str, Any]) -> dict[str, object]:
    return redact_provider_error_details(payload)


@dataclass(frozen=True, slots=True)
class ParsedProviderError:
    kind: ProviderErrorKind
    message: str
    details: dict[str, object]
    #: ``None`` means the kind does not decide it: ``rate_limit`` is a usage/limit
    #: answer whose lane the runtime's own policy owns.
    retryable: bool | None
    fallback_allowed: bool
    retry_after: float | None
    guidance: str


def _recovery_policy_for_kind(kind: ProviderErrorKind) -> tuple[bool | None, bool]:
    if kind == "context_limit":
        return False, False
    if kind in {
        "missing_auth",
        "invalid_model",
        "not_configured",
        "unsupported_feature",
        "stream_tool_feedback_shape",
    }:
        return False, True
    if kind == "rate_limit":
        # A usage/limit answer is not a provider-lane retry: OMP's
        # ``isProviderRetryableError`` returns false for ``isUsageLimit`` and hands
        # both 402 and 429 to credential rotation (``error/retryable.ts:45-46``,
        # ``error/rate-limit.ts:354-376``). voidcode has no rotation, so the decision
        # belongs to the runtime policy (its rate-limit lane, else fallback) rather
        # than to a parse-time ``True``.
        return None, True
    if kind == "cancelled":
        return False, False
    return True, True


def guidance_for_provider_error_kind(kind: ProviderErrorKind) -> str:
    if kind == "missing_auth":
        return "Configure the provider API key or auth method, then retry."
    if kind == "invalid_model":
        return "Check the configured provider/model name and model access permissions."
    if kind == "not_configured":
        return "Set providers.<name>.base_url (and the provider API key) before using this provider."
    if kind == "rate_limit":
        return "Retry later, reduce request volume, or configure a fallback model."
    if kind == "context_limit":
        return "Reduce prompt/tool-result context or switch to a model with a larger context window."
    if kind == "unsupported_feature":
        return "Disable the unsupported provider feature or choose a model/provider that supports it."
    if kind == "stream_tool_feedback_shape":
        return "Report this provider stream/tool-call shape; VoidCode could not normalize it safely."
    if kind == "cancelled":
        return "The request was cancelled; rerun when ready."
    return "Retry the request or configure a fallback provider/model."


def _extract_error_message(payload: dict[str, Any]) -> str | None:
    direct = payload.get("message")
    if isinstance(direct, str) and direct.strip():
        if _SDK_ERROR_WRAPPER_PREFIX.match(direct.strip()) is None:
            return direct
        # The SDK synthesised `Error code: <status> - <body>`; prefer the provider's own text.
        return _nested_error_message(payload) or _sdk_error_wrapper_body(direct) or direct
    return _nested_error_message(payload)


def _extract_error_code(payload: dict[str, Any]) -> str | None:
    code = payload.get("code")
    if isinstance(code, str) and code.strip():
        return code
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        error_payload = dict(error_obj)
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


#: The header names OMP reads for a retry hint; the longest parsed value wins
#: (``packages/ai/src/utils/retry-after.ts:31-43``).
_RETRY_AFTER_HEADERS = frozenset({"retry-after-ms", "retry-after", "x-ratelimit-reset-ms", "x-ratelimit-reset"})
_RESET_HEADERS = frozenset({"x-ratelimit-reset-ms", "x-ratelimit-reset"})
_MS_PER_SECOND = 1000.0
_EPOCH_MS_FLOOR = 1_000_000_000_000.0
_EPOCH_SECONDS_FLOOR = 1_000_000_000.0

_UNIT_SECONDS: dict[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}


def _retry_after_from_value(raw: object) -> float | None:
    """Seconds from one raw hint (a number, a numeric string, or an HTTP date)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if not isinstance(raw, str):
        return None
    try:
        return float(raw)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except TypeError, ValueError, OverflowError:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return (retry_at - datetime.now(UTC)).total_seconds()


def _reset_header_seconds(raw: object, *, milliseconds: bool) -> float | None:
    """An ``x-ratelimit-reset*`` counter, read the way OMP reads it
    (``utils/retry-after.ts:104-125``): a delta unless the number is large enough to
    be an absolute epoch (milliseconds first, then seconds). A counter that has
    already elapsed is not a hint; only ``retry-after``'s own zero is a retry-now
    signal.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    now = datetime.now(UTC).timestamp()
    if value <= 0:
        return None
    if value > _EPOCH_MS_FLOOR:
        delta = value / _MS_PER_SECOND - now
    elif value > _EPOCH_SECONDS_FLOOR:
        delta = value - now
    else:
        delta = value / _MS_PER_SECOND if milliseconds else value
    return delta if delta > 0 else None


def _retry_after_from_header(header: str, raw: object) -> float | None:
    """Seconds from one header value, in the unit its own name declares."""
    if header in _RESET_HEADERS:
        return _reset_header_seconds(raw, milliseconds=header.endswith("-ms"))
    value = _retry_after_from_value(raw)
    if value is None:
        return None
    if header == "retry-after-ms":
        # The millisecond header is a plain positive delta: OMP's
        # ``parseRetryAfterMsHeader`` drops anything <= 0 (``utils/retry-after.ts:79-85``).
        # Only the body's ``retry-after-ms=0`` spelling is a retry-now signal.
        return value / _MS_PER_SECOND if value > 0 else None
    return value


def _amount_seconds(match: re.Match[str], *, default_unit: str = "s") -> float | None:
    value = float(match.group(1))
    if value <= 0:
        return None
    unit = match.group(2) if match.lastindex and match.lastindex > 1 else default_unit
    return value * _UNIT_SECONDS[unit.lower()]


def _reset_after_seconds(match: re.Match[str]) -> float | None:
    hours, minutes, seconds = (float(group or 0) for group in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def _hour_minute_seconds(match: re.Match[str]) -> float | None:
    hours, minutes = (float(group) for group in match.groups())
    total = hours * 3600 + minutes * 60
    return total if total > 0 else None


def _millisecond_body_seconds(match: re.Match[str]) -> float:
    """``retry-after-ms`` in the text: a parsed zero is a retry-now signal."""
    return int(match.group(1)) / _MS_PER_SECOND


#: One body-text timing pattern and the wait its match reports. OMP reads a body by
#: evaluating every pattern and keeping the longest wait, and a gateway that reports
#: the wait only in the message is common enough to matter
#: (``packages/utils/src/fetch-retry.ts:4-29,118-215``).
_BODY_RETRY_HINTS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], float | None]], ...] = (
    (re.compile(r"retry-after-ms\s*[:=]\s*([0-9]+)", re.IGNORECASE), _millisecond_body_seconds),
    (re.compile(r"reset after (?:(\d+)h)?(?:(\d+)m)?([0-9.]+)s", re.IGNORECASE), _reset_after_seconds),
    (re.compile(r"please retry in ([0-9.]+)\s*(ms|s)\b", re.IGNORECASE), _amount_seconds),
    (re.compile(r'"retryDelay"\s*:\s*"([0-9.]+)\s*(ms|s)"', re.IGNORECASE), _amount_seconds),
    (re.compile(r"try again in\s+~?\s*([0-9.]+)\s*(ms|sec|s|minutes?|mins?|m|hours?|hrs?|h)\b", re.IGNORECASE), _amount_seconds),
    (re.compile(r"(?:will\s+)?resets?\s+in\s+~?\s*([0-9.]+)\s*(ms|sec|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", re.IGNORECASE), _amount_seconds),
    # The compound remainder the generic pattern above truncates: "Resets in 2hr 15min".
    (re.compile(r"resets?\s+in\s+~?\s*(\d+(?:\.\d+)?)\s*hr\s*(\d+(?:\.\d+)?)\s*min\b", re.IGNORECASE), _hour_minute_seconds),
)

#: ``reset at`` wall clocks (``utils/fetch-retry.ts:18-25``). voidcode ships no
#: per-provider reset timezone, so a stamp that omits its zone is read as UTC -- the
#: same reading the HTTP-date header path uses -- and only settles the wait when no
#: relative signal exists, which is what OMP does with a naive stamp
#: (``utils/fetch-retry.ts:135-165,241``).
_RESET_AT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:will\s+)?reset at\s+([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)",
        re.IGNORECASE,
    ),
    re.compile(r"将在\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}:[0-9]{2})\s*重置"),
)
_ZONE_SUFFIX = re.compile(r"(?:Z|[+-][0-9]{2}:?[0-9]{2})$", re.IGNORECASE)


def _body_retry_seconds(message: str) -> tuple[list[float], list[float]]:
    """The waits a message text reports, as ``(relative, reset-at fallback)``."""
    relative: list[float] = []
    fallback: list[float] = []
    for pattern, seconds in _BODY_RETRY_HINTS:
        match = pattern.search(message)
        if match is None:
            continue
        value = seconds(match)
        if value is not None:
            relative.append(value)
    for pattern in _RESET_AT_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        raw = match.group(1).replace(" ", "T")
        zone_aware = _ZONE_SUFFIX.search(raw) is not None
        try:
            retry_at = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if not zone_aware:
            retry_at = retry_at.replace(tzinfo=UTC)
        delta = (retry_at - datetime.now(UTC)).total_seconds()
        if delta > 0:
            (relative if zone_aware else fallback).append(delta)
    return relative, fallback


def _extract_retry_after(payload: dict[str, Any]) -> float | None:
    """The longest wait any hint in ``payload`` reports, capped at one hour.

    Every header name is parsed and the maximum across them wins
    (``utils/retry-after.ts:31-43``); the message text competes in the same maximum,
    because a parsed ``0`` is a retry-now signal that must survive rather than be
    dropped in favour of a longer reading. The 3600s cap is voidcode's own (OMP fails
    fast above its ``maxDelayMs`` instead).
    """
    candidates: list[float] = []
    for key in ("retry_after", "retry-after"):
        direct = _retry_after_from_value(payload.get(key))
        if direct is not None:
            candidates.append(direct)
    headers = payload.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            header = str(key).lower()
            if header not in _RETRY_AFTER_HEADERS:
                continue
            parsed = _retry_after_from_header(header, value)
            if parsed is not None:
                candidates.append(parsed)
    relative, fallback = _body_retry_seconds(_extract_error_message(payload) or "")
    candidates.extend(relative)
    if not candidates:
        candidates.extend(fallback)
    if not candidates:
        return None
    return min(max(candidates), 3600.0)


def _is_context_overflow(message: str, status_code: int | None, code: str | None) -> bool:
    if status_code == 413:
        return True
    if code is not None and code.lower() == "context_length_exceeded":
        return True
    return any(pattern.search(message) is not None for pattern in _CONTEXT_OVERFLOW_PATTERNS)


# Message markers, in the order they are tried: the first table whose marker
# appears in the lowered message decides the kind.
_MESSAGE_MARKER_KINDS: tuple[tuple[ProviderErrorKind, tuple[str, ...]], ...] = (
    (
        "invalid_model",
        ("insufficient balance", "insufficient quota", "quota exceeded", "billing balance", "payment required"),
    ),
    (
        "missing_auth",
        ("api key is missing", "missing api key", "invalid api key", "authentication failed", "unauthorized"),
    ),
    (
        "unsupported_feature",
        (
            "unsupported stream",
            "streaming is not supported",
            "tools are not supported",
            "tool calling is not supported",
            "invalid schema for function",
            "invalid function schema",
        ),
    ),
    (
        "stream_tool_feedback_shape",
        ("invalid tool call", "tool_calls must", "malformed tool call", "stream tool"),
    ),
)
_MODEL_ACCESS_MARKERS = ("not authorized for model", "model access", "model unavailable", "permission to access model", "usage not included")


def _kind_from_message_markers(lowered_message: str) -> ProviderErrorKind | None:
    return next(
        (kind for kind, markers in _MESSAGE_MARKER_KINDS if any(marker in lowered_message for marker in markers)),
        None,
    )


def _classify_api_error_kind(
    *,
    message: str,
    status_code: int | None,
    code: str | None,
) -> ProviderErrorKind:
    if _is_context_overflow(message, status_code, code):
        return "context_limit"

    normalized_code = None if code is None else code.lower()
    if status_code in {402, 429} or normalized_code in {"rate_limit", "rate_limit_exceeded", "insufficient_balance", "payment_required"}:
        # 402 and 429 are both usage/limit errors (OMP ``isUsageLimitStatus``,
        # ``error/rate-limit.ts:321-323``): the runtime's own retry lane, never a
        # transient provider failure.
        return "rate_limit"

    if normalized_code in {
        "missing_api_key",
        "invalid_api_key",
        "authentication_error",
        "unauthorized",
    }:
        return "missing_auth"

    if normalized_code in {
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
    marker_kind = _kind_from_message_markers(lowered_message)
    if marker_kind is not None:
        return marker_kind

    if status_code == 403:
        if any(pattern.search(message) is not None for pattern in _INVALID_MODEL_PATTERNS):
            return "invalid_model"
        if any(marker in lowered_message for marker in _MODEL_ACCESS_MARKERS):
            return "invalid_model"
        return "missing_auth"

    if any(pattern.search(message) is not None for pattern in _INVALID_MODEL_PATTERNS):
        return "invalid_model"

    if status_code is not None and status_code >= 500:
        return "transient_failure"

    return "transient_failure"


def parse_provider_api_error(payload: dict[str, Any]) -> ParsedProviderError:
    return _parse_provider_error(payload, source="api", default_message="provider api error")


def parse_provider_stream_error(payload: dict[str, Any]) -> ParsedProviderError:
    return _parse_provider_error(payload, source="stream", default_message="provider stream error")


def _is_terminal_client_status(status_code: int | None) -> bool:
    """Whether a status is a permanent client error: 4xx except 408 and 429."""
    return status_code is not None and 400 <= status_code < 500 and status_code not in {408, 429}


def _parse_provider_error(payload: dict[str, Any], *, source: str, default_message: str) -> ParsedProviderError:
    message = _extract_error_message(payload) or default_message
    status_code = _extract_status_code(payload)
    code = _extract_error_code(payload)
    kind = _classify_api_error_kind(message=message, status_code=status_code, code=code)
    retryable, fallback_allowed = _recovery_policy_for_kind(kind)
    if _is_terminal_client_status(status_code):
        # OMP: every 4xx except 408/429 is terminal (``error/retryable.ts:44-63``),
        # so a permanent client error is never retried -- whatever kind its message
        # markers happened to match. Fallback stays allowed: another provider in the
        # chain may serve the request.
        retryable = False
    details = _provider_error_details(payload)
    guidance = guidance_for_provider_error_kind(kind)
    retry_after = _extract_retry_after(payload)
    details.update(
        {
            "source": source,
            "status_code": status_code,
            "error_code": code,
            "guidance": guidance,
        }
    )
    return ParsedProviderError(
        kind=kind,
        message=redact_provider_error_message(message),
        details=details,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        retry_after=retry_after,
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
        fallback_allowed=parsed.fallback_allowed,
        retry_after=parsed.retry_after,
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
        fallback_allowed=parsed.fallback_allowed,
        retry_after=parsed.retry_after,
        details=parsed.details,
    )


class ProviderError(ValueError):
    """Base runtime-classified provider error."""


class ProviderContextLimitError(ProviderError):
    """Provider failure caused by context window exhaustion."""


def format_invalid_provider_config_error(field_path: str, reason: str) -> str:
    return f"invalid provider config: {field_path} {reason}"


def validation_reason_from_error(error: Mapping[str, object]) -> str:
    """Read the user-facing reason out of one pydantic error dict.

    ``value_error`` errors wrap the original exception in ``ctx.error``; every
    other error reports its ``msg`` (defaulting to ``"is invalid"``).
    """
    error_type = error.get("type", "")
    if error_type == "value_error":
        context = error.get("ctx")
        if isinstance(context, dict):
            nested_error = context.get("error")
            if isinstance(nested_error, ValueError):
                return str(nested_error)
    message = error.get("msg", "is invalid")
    return message if isinstance(message, str) else "is invalid"


def classify_provider_error(exc: Exception) -> ProviderError | None:
    message = str(exc).lower()
    context_limit_markers = (
        "context window",
        "context limit",
        "maximum context",
        "maximum context length",
        "token limit",
    )
    if any(marker in message for marker in context_limit_markers):
        return ProviderContextLimitError(str(exc))
    return None
