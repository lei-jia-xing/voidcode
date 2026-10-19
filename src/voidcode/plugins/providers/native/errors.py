from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from voidcode.sdk.provider import ProviderErrorKind, ProviderExecutionError

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
        # The runtime owns bounded pruning and larger-window fallback policy.
        return None, True
    if kind in {
        "missing_auth",
        "invalid_model",
        "not_configured",
        "provider_error",
        "unsupported_feature",
        "stream_tool_feedback_shape",
    }:
        return False, True
    if kind == "rate_limit":
        # The runtime's rate-limit lane owns deferral; otherwise use configured fallback.
        return None, True
    if kind == "cancelled":
        return False, False
    return True, True


def guidance_for_provider_error_kind(kind: str) -> str:
    """Guidance for a provider-error kind token, generic for one nothing recognizes."""
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
    if kind == "provider_error":
        return "Inspect the structured provider error details or configure a fallback provider/model."
    if kind == "unsupported_feature":
        return "Disable the unsupported provider feature or choose a model/provider that supports it."
    if kind == "stream_tool_feedback_shape":
        return "Report this provider stream/tool-call shape; VoidCode could not normalize it safely."
    if kind == "cancelled":
        return "The request was cancelled; rerun when ready."
    return "Inspect the provider error details or configure a fallback provider/model."


def _classify_api_error_kind(*, status_code: int | None, code: str | None) -> ProviderErrorKind:
    normalized_code = None if code is None else code.lower()
    if normalized_code in {"context_length_exceeded", "model_context_window_exceeded"}:
        return "context_limit"
    if normalized_code in {"rate_limit", "rate_limit_exceeded"} or status_code == 429:
        return "rate_limit"
    if normalized_code == "missing_api_key":
        return "missing_auth"
    if normalized_code in {"invalid_model", "model_not_found"}:
        return "invalid_model"
    return "provider_error"


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
    kind = _classify_api_error_kind(status_code=status_code, code=code)
    retryable, fallback_allowed = _recovery_policy_for_kind(kind)
    if payload.get("network_failure") is True:
        # SDK-recognized connection/time-out classes opt into retry without
        # relabeling the causal provider error as a generic transient failure.
        retryable = True
    elif status_code == 408 or (status_code is not None and status_code >= 500):
        # These statuses authorize retry policy; they do not classify the cause as transient.
        retryable = True
    elif _is_terminal_client_status(status_code):
        retryable = False
    details = dict(payload)
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
        message=_strip_provider_error_wrapper(message),
        details=details,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        retry_after=retry_after,
        guidance=guidance,
    )


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
