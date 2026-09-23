from __future__ import annotations

import re
from typing import Final

#: Canonical placeholder for redacted secret text/values.
REDACTED_PLACEHOLDER: Final[str] = "[redacted]"

#: Canonical credential blocklist shared by every redactor. Deliberately
#: excludes ``prompt``/``env``/``skill_body``: those were policy-diagnostics-only
#: fragments, and matching them session-wide nukes structural keys such as
#: ``prompt_activation`` and ``prompt`` (breaks bundle roundtrips).
SECRET_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "secret",
    "session_token",
    "token",
)

#: Policy-diagnostics legacy extension. The policy surface historically redacted
#: prompt/env/skill_body keys (pinned by policy diagnostics tests); it is the
#: only surface that applies this extension.
POLICY_EXTRA_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "env",
    "prompt",
    "skill_body",
)

#: Single bearer/sk-* + key=value regex set. Key=value patterns keep the key
#: prefix (``\1``) so diagnostics stay greppable; sk-* has no key to keep.
SECRET_TEXT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(access[_-]?token\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(client[_-]?secret\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(secret\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]+"),
)

#: Per-surface limits live here so call sites share one truncate() path.
DIAGNOSTIC_TEXT_CHARS: Final[int] = 4000
DIAGNOSTIC_DEPTH: Final[int] = 8
DIAGNOSTIC_ITEMS: Final[int] = 256
DEBUG_CONTENT_CHARS: Final[int] = 2_000
PROMPT_FRAGMENT_PREVIEW_CHARS: Final[int] = 240
PROMPT_ACTIVATION_PREVIEW_CHARS: Final[int] = 160
POLICY_DIAGNOSTIC_CHARS: Final[int] = 256
POLICY_DIAGNOSTIC_LIMIT: Final[int] = 32
BUNDLE_TOOL_OUTPUT_PREVIEW_CHARS: Final[int] = 2_000
TOOL_OUTPUT_LINES: Final[int] = 2000
TOOL_OUTPUT_BYTES: Final[int] = 50 * 1024
MODEL_FIELD_CHARS: Final[int] = 4000


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def redact_text(value: str, *, placeholder: str = REDACTED_PLACEHOLDER) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{placeholder}" if match.lastindex else placeholder, redacted)
    return redacted


def redact_value(value: object, *, placeholder: str = REDACTED_PLACEHOLDER) -> object:
    if isinstance(value, str):
        return redact_text(value, placeholder=placeholder)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if is_sensitive_key(key):
                result[key] = placeholder
            else:
                result[key] = redact_value(raw_item, placeholder=placeholder)
        return result
    if isinstance(value, list):
        return [redact_value(item, placeholder=placeholder) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, placeholder=placeholder) for item in value)
    return value


def redact(value: object, *, placeholder: str = REDACTED_PLACEHOLDER) -> object:
    return redact_value(value, placeholder=placeholder)


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated: kept first {limit} of {len(text)} chars]"


__all__ = [
    "BUNDLE_TOOL_OUTPUT_PREVIEW_CHARS",
    "DEBUG_CONTENT_CHARS",
    "DIAGNOSTIC_DEPTH",
    "DIAGNOSTIC_ITEMS",
    "DIAGNOSTIC_TEXT_CHARS",
    "MODEL_FIELD_CHARS",
    "POLICY_DIAGNOSTIC_CHARS",
    "POLICY_DIAGNOSTIC_LIMIT",
    "POLICY_EXTRA_KEY_FRAGMENTS",
    "PROMPT_FRAGMENT_PREVIEW_CHARS",
    "REDACTED_PLACEHOLDER",
    "SECRET_KEY_FRAGMENTS",
    "SECRET_TEXT_PATTERNS",
    "TOOL_OUTPUT_BYTES",
    "TOOL_OUTPUT_LINES",
    "is_sensitive_key",
    "redact",
    "redact_text",
    "redact_value",
    "truncate",
]
