from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast

from ..tools.contracts import ToolResult


def _empty_tool_limits() -> dict[str, int]:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeContinuityState:
    summary_text: str | None = None
    dropped_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    source: str = "tool_result_window"
    original_tool_result_tokens: int | None = None
    retained_tool_result_tokens: int | None = None
    dropped_tool_result_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None
    # Lightweight versioning for continuity state to aid reinjection/refresh
    # semantics. This is incremented when the shape evolves and is included
    # in the serialized payload so consumers can decide how to handle newer
    # fields.
    version: int = 1

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "summary_text": self.summary_text,
            "dropped_tool_result_count": self.dropped_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "source": self.source,
            "version": self.version,
        }
        if self.original_tool_result_tokens is not None:
            payload["original_tool_result_tokens"] = self.original_tool_result_tokens
        if self.retained_tool_result_tokens is not None:
            payload["retained_tool_result_tokens"] = self.retained_tool_result_tokens
        if self.dropped_tool_result_tokens is not None:
            payload["dropped_tool_result_tokens"] = self.dropped_tool_result_tokens
        if self.token_budget is not None:
            payload["token_budget"] = self.token_budget
        if self.token_estimate_source is not None:
            payload["token_estimate_source"] = self.token_estimate_source
        return payload


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    auto_compaction: bool = True
    max_tool_results: int = 4
    max_tool_result_tokens: int | None = None
    max_context_ratio: float | None = None
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    minimum_retained_tool_results: int = 1
    recent_tool_result_count: int = 1
    recent_tool_result_tokens: int | None = None
    default_tool_result_tokens: int | None = None
    per_tool_result_tokens: Mapping[str, int] = field(default_factory=_empty_tool_limits)
    tokenizer_model: str | None = None
    continuity_preview_items: int = 3
    continuity_preview_chars: int = 80

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_tool_result_tokens", dict(self.per_tool_result_tokens))
        if self.max_tool_results < 0:
            raise ValueError("max_tool_results must be >= 0")
        if self.max_tool_result_tokens is not None and self.max_tool_result_tokens < 1:
            raise ValueError("max_tool_result_tokens must be >= 1 when provided")
        if self.max_context_ratio is not None and not 0 < self.max_context_ratio <= 1:
            raise ValueError("max_context_ratio must be > 0 and <= 1 when provided")
        if self.model_context_window_tokens is not None and self.model_context_window_tokens < 1:
            raise ValueError("model_context_window_tokens must be >= 1 when provided")
        if self.reserved_output_tokens is not None and self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be >= 0 when provided")
        if self.minimum_retained_tool_results < 0:
            raise ValueError("minimum_retained_tool_results must be >= 0")
        if self.recent_tool_result_count < 0:
            raise ValueError("recent_tool_result_count must be >= 0")
        if self.recent_tool_result_tokens is not None and self.recent_tool_result_tokens < 1:
            raise ValueError("recent_tool_result_tokens must be >= 1 when provided")
        if self.default_tool_result_tokens is not None and self.default_tool_result_tokens < 1:
            raise ValueError("default_tool_result_tokens must be >= 1 when provided")
        for tool_name, limit in self.per_tool_result_tokens.items():
            if not tool_name:
                raise ValueError("per_tool_result_tokens tool names must be non-empty")
            if limit < 1:
                raise ValueError("per_tool_result_tokens limits must be >= 1")
        if self.tokenizer_model is not None and not self.tokenizer_model:
            raise ValueError("tokenizer_model must be non-empty when provided")
        if self.continuity_preview_items < 1:
            raise ValueError("continuity_preview_items must be >= 1")
        if self.continuity_preview_chars < 1:
            raise ValueError("continuity_preview_chars must be >= 1")

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": 1,
            "auto_compaction": self.auto_compaction,
            "max_tool_results": self.max_tool_results,
            "minimum_retained_tool_results": self.minimum_retained_tool_results,
            "recent_tool_result_count": self.recent_tool_result_count,
            "continuity_preview_items": self.continuity_preview_items,
            "continuity_preview_chars": self.continuity_preview_chars,
        }
        if self.max_tool_result_tokens is not None:
            payload["max_tool_result_tokens"] = self.max_tool_result_tokens
        if self.max_context_ratio is not None:
            payload["max_context_ratio"] = self.max_context_ratio
        if self.model_context_window_tokens is not None:
            payload["model_context_window_tokens"] = self.model_context_window_tokens
        if self.reserved_output_tokens is not None:
            payload["reserved_output_tokens"] = self.reserved_output_tokens
        if self.recent_tool_result_tokens is not None:
            payload["recent_tool_result_tokens"] = self.recent_tool_result_tokens
        if self.default_tool_result_tokens is not None:
            payload["default_tool_result_tokens"] = self.default_tool_result_tokens
        if self.per_tool_result_tokens:
            payload["per_tool_result_tokens"] = dict(self.per_tool_result_tokens)
        if self.tokenizer_model is not None:
            payload["tokenizer_model"] = self.tokenizer_model
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    max_tool_result_count: int = 0
    original_tool_result_tokens: int | None = None
    retained_tool_result_tokens: int | None = None
    dropped_tool_result_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None
    reserved_output_tokens: int | None = None
    truncated_tool_result_count: int = 0
    continuity_state: RuntimeContinuityState | None = None
    summary_anchor: str | None = None
    summary_source: dict[str, int] | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "compacted": self.compacted,
            "compaction_reason": self.compaction_reason,
            "original_tool_result_count": self.original_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "max_tool_result_count": self.max_tool_result_count,
        }
        if self.original_tool_result_tokens is not None:
            payload["original_tool_result_tokens"] = self.original_tool_result_tokens
        if self.retained_tool_result_tokens is not None:
            payload["retained_tool_result_tokens"] = self.retained_tool_result_tokens
        if self.dropped_tool_result_tokens is not None:
            payload["dropped_tool_result_tokens"] = self.dropped_tool_result_tokens
        if self.token_budget is not None:
            payload["token_budget"] = self.token_budget
        if self.token_estimate_source is not None:
            payload["token_estimate_source"] = self.token_estimate_source
        if self.reserved_output_tokens is not None:
            payload["reserved_output_tokens"] = self.reserved_output_tokens
        if self.truncated_tool_result_count:
            payload["truncated_tool_result_count"] = self.truncated_tool_result_count
        if self.continuity_state is not None:
            payload["continuity_state"] = self.continuity_state.metadata_payload()
        if self.summary_anchor is not None:
            payload["summary_anchor"] = self.summary_anchor
        if self.summary_source is not None:
            payload["summary_source"] = dict(self.summary_source)
        return payload


def _tool_result_preview(result: ToolResult, *, max_preview_chars: int) -> str:
    parts = [result.tool_name, result.status]
    path = result.data.get("path")
    if isinstance(path, str) and path:
        parts.append(f"path={path}")
    pattern = result.data.get("pattern")
    if isinstance(pattern, str) and pattern:
        parts.append(f"pattern={pattern}")
    command = result.data.get("command")
    if isinstance(command, str) and command:
        parts.append(f"command={command}")

    content = normalize_read_file_output(result.content)
    error = result.error.strip() if result.error else ""
    preview_source = content or error
    if preview_source:
        clipped = preview_source[:max_preview_chars]
        if len(preview_source) > max_preview_chars:
            clipped = f"{clipped}..."
        preview_label = "content_preview" if content else "error_preview"
        parts.append(f'{preview_label}="{clipped}"')
    return " ".join(parts)


_UNICODE_TOKEN_ESTIMATE_SOURCE = "unicode_aware_chars"


class _TokenEstimate(NamedTuple):
    tokens: int
    source: str


def _estimated_token_count(value: str, *, tokenizer_model: str | None = None) -> _TokenEstimate:
    if not value:
        return _TokenEstimate(0, _UNICODE_TOKEN_ESTIMATE_SOURCE)
    if tokenizer_model is not None:
        try:
            tiktoken = cast(Any, importlib.import_module("tiktoken"))

            try:
                encoding = tiktoken.encoding_for_model(tokenizer_model)
            except KeyError:
                encoding = tiktoken.get_encoding("cl100k_base")
            return _TokenEstimate(
                len(encoding.encode(value, disallowed_special=())),
                f"tiktoken:{tokenizer_model}",
            )
        except ImportError:
            pass
    ascii_chars = 0
    non_ascii_chars = 0
    for char in value:
        if ord(char) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return _TokenEstimate(
        max(1, ((ascii_chars + 3) // 4) + non_ascii_chars),
        _UNICODE_TOKEN_ESTIMATE_SOURCE,
    )


def _tool_result_token_estimate(
    result: ToolResult, *, tokenizer_model: str | None = None
) -> _TokenEstimate:
    payload = {
        "tool_name": result.tool_name,
        "status": result.status,
        "content": normalize_tool_result_content(result.content),
        "error": result.error,
        "data": result.data,
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return _estimated_token_count(serialized, tokenizer_model=tokenizer_model)


def _policy_token_budget(policy: ContextWindowPolicy) -> int | None:
    if policy.max_tool_result_tokens is not None:
        return policy.max_tool_result_tokens
    if policy.model_context_window_tokens is None:
        return None
    available_context = policy.model_context_window_tokens
    if policy.reserved_output_tokens is not None:
        available_context = max(1, available_context - policy.reserved_output_tokens)
    if policy.max_context_ratio is None:
        return available_context if policy.reserved_output_tokens is not None else None
    return max(1, int(available_context * policy.max_context_ratio))


def _tool_limit_for_result(result: ToolResult, policy: ContextWindowPolicy) -> int | None:
    return policy.per_tool_result_tokens.get(result.tool_name, policy.default_tool_result_tokens)


def _clip_plain_text_to_token_limit(text: str, *, limit: int, tokenizer_model: str | None) -> str:
    clipped: list[str] = []
    used = 0
    for char in text:
        char_tokens = _estimated_token_count(char, tokenizer_model=tokenizer_model).tokens
        if used + char_tokens > limit:
            break
        clipped.append(char)
        used += char_tokens
    candidate = "".join(clipped)
    while (
        candidate
        and _estimated_token_count(candidate, tokenizer_model=tokenizer_model).tokens > limit
    ):
        candidate = candidate[:-1]
    return candidate


def _truncation_message(*, omitted_chars: int) -> str:
    return f"\n[Tool output truncated by context window policy; omitted {omitted_chars} chars]"


def _clip_text_to_token_limit(text: str, *, limit: int, tokenizer_model: str | None) -> str:
    if _estimated_token_count(text, tokenizer_model=tokenizer_model).tokens <= limit:
        return text
    clipped = _clip_plain_text_to_token_limit(
        text,
        limit=limit,
        tokenizer_model=tokenizer_model,
    )
    while True:
        omitted = len(text) - len(clipped)
        truncation_message = _truncation_message(omitted_chars=omitted)
        candidate = f"{clipped}{truncation_message}"
        if _estimated_token_count(candidate, tokenizer_model=tokenizer_model).tokens <= limit:
            return candidate
        if not clipped:
            return _clip_plain_text_to_token_limit(
                truncation_message,
                limit=limit,
                tokenizer_model=tokenizer_model,
            )
        clipped = clipped[:-1]


def _truncate_tool_result_content(
    result: ToolResult,
    *,
    limit: int | None,
    tokenizer_model: str | None,
) -> tuple[ToolResult, bool]:
    if limit is None or result.content is None:
        return result, False
    original_estimate = _estimated_token_count(
        result.content,
        tokenizer_model=tokenizer_model,
    )
    if original_estimate.tokens <= limit:
        return result, False
    clipped = _clip_text_to_token_limit(
        result.content,
        limit=limit,
        tokenizer_model=tokenizer_model,
    )
    data = {
        **result.data,
        "context_window_truncated": True,
        "context_window_original_content_tokens": original_estimate.tokens,
        "context_window_content_token_limit": limit,
    }
    return (
        ToolResult(
            tool_name=result.tool_name,
            status=result.status,
            content=clipped,
            data=data,
            error=result.error,
            truncated=True,
            partial=True,
            attachment=result.attachment,
            timeout_seconds=result.timeout_seconds,
            source=result.source,
            fallback_reason=result.fallback_reason,
            reference=result.reference,
            error_kind=result.error_kind,
        ),
        True,
    )


def _retain_results_within_token_budget(
    tool_results: tuple[ToolResult, ...],
    *,
    token_budget: int,
    minimum_retained_results: int,
    tokenizer_model: str | None,
) -> tuple[ToolResult, ...]:
    if not tool_results:
        return ()

    retained_reversed: list[ToolResult] = []
    retained_tokens = 0
    minimum_count = min(minimum_retained_results, len(tool_results))
    for result in reversed(tool_results):
        estimate = _tool_result_token_estimate(result, tokenizer_model=tokenizer_model).tokens
        must_retain = len(retained_reversed) < minimum_count
        if not must_retain and retained_tokens + estimate > token_budget:
            break
        retained_reversed.append(result)
        retained_tokens += estimate
    return tuple(reversed(retained_reversed))


def _token_estimate_source(policy: ContextWindowPolicy, sample: str = "sample") -> str:
    return _estimated_token_count(sample, tokenizer_model=policy.tokenizer_model).source


def _coerce_optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"context window policy field '{key}' must be an integer")


def _coerce_int(payload: Mapping[str, object], key: str, *, default: int) -> int:
    if key not in payload:
        return default
    value = _coerce_optional_int(payload, key)
    if value is None:
        raise ValueError(f"context window policy field '{key}' must be an integer")
    return value


def context_window_policy_from_payload(raw_payload: object) -> ContextWindowPolicy:
    if not isinstance(raw_payload, dict):
        raise ValueError("context window policy payload must be an object")
    payload = cast(dict[str, object], raw_payload)
    per_tool_raw = payload.get("per_tool_result_tokens")
    per_tool: dict[str, int] = {}
    if per_tool_raw is not None:
        if not isinstance(per_tool_raw, dict):
            raise ValueError(
                "context window policy field 'per_tool_result_tokens' must be an object"
            )
        for key, value in cast(dict[object, object], per_tool_raw).items():
            if not isinstance(key, str) or not key:
                raise ValueError("context window policy per-tool keys must be non-empty strings")
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("context window policy per-tool limits must be positive integers")
            per_tool[key] = value
    auto_compaction = payload.get("auto_compaction", True)
    if not isinstance(auto_compaction, bool):
        raise ValueError("context window policy field 'auto_compaction' must be a boolean")
    max_context_ratio = payload.get("max_context_ratio")
    if max_context_ratio is not None and not isinstance(max_context_ratio, int | float):
        raise ValueError("context window policy field 'max_context_ratio' must be a number")
    tokenizer_model = payload.get("tokenizer_model")
    if tokenizer_model is not None and not isinstance(tokenizer_model, str):
        raise ValueError("context window policy field 'tokenizer_model' must be a string")
    return ContextWindowPolicy(
        auto_compaction=auto_compaction,
        max_tool_results=_coerce_int(
            payload,
            "max_tool_results",
            default=ContextWindowPolicy().max_tool_results,
        ),
        max_tool_result_tokens=_coerce_optional_int(payload, "max_tool_result_tokens"),
        max_context_ratio=float(max_context_ratio) if max_context_ratio is not None else None,
        model_context_window_tokens=_coerce_optional_int(payload, "model_context_window_tokens"),
        reserved_output_tokens=_coerce_optional_int(payload, "reserved_output_tokens"),
        minimum_retained_tool_results=_coerce_int(
            payload,
            "minimum_retained_tool_results",
            default=ContextWindowPolicy().minimum_retained_tool_results,
        ),
        recent_tool_result_count=_coerce_int(
            payload,
            "recent_tool_result_count",
            default=ContextWindowPolicy().recent_tool_result_count,
        ),
        recent_tool_result_tokens=_coerce_optional_int(payload, "recent_tool_result_tokens"),
        default_tool_result_tokens=_coerce_optional_int(payload, "default_tool_result_tokens"),
        per_tool_result_tokens=per_tool,
        tokenizer_model=tokenizer_model,
        continuity_preview_items=_coerce_int(
            payload,
            "continuity_preview_items",
            default=ContextWindowPolicy().continuity_preview_items,
        ),
        continuity_preview_chars=_coerce_int(
            payload,
            "continuity_preview_chars",
            default=ContextWindowPolicy().continuity_preview_chars,
        ),
    )


def normalize_tool_result_content(content: str | None) -> str | None:
    if not content:
        return content

    return normalize_read_file_output(content)


def normalize_read_file_output(content: str | None) -> str | None:
    if not content:
        return content

    stripped = content.strip()
    if not (stripped.startswith("<path>") and "<content>" in stripped and "</content>" in stripped):
        return content

    body_start = stripped.find("<content>") + len("<content>")
    body_end = stripped.rfind("</content>")
    body = stripped[body_start:body_end].strip()
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("("):
            if line.startswith("(Showing lines ") or line.startswith("(Output capped at "):
                lines.append(line)
            continue
        if ": " in raw_line:
            _, text = raw_line.split(": ", 1)
            lines.append(text)
            continue
        lines.append(line)
    return "\n".join(lines)


def _build_continuity_state(
    *,
    dropped_results: tuple[ToolResult, ...],
    retained_count: int,
    preview_item_limit: int,
    preview_char_limit: int,
    original_tokens: int | None = None,
    retained_tokens: int | None = None,
    dropped_tokens: int | None = None,
    token_budget: int | None = None,
    token_estimate_source: str | None = None,
) -> RuntimeContinuityState:
    dropped_count = len(dropped_results)
    if dropped_count == 0:
        return RuntimeContinuityState(
            retained_tool_result_count=retained_count,
            original_tool_result_tokens=original_tokens,
            retained_tool_result_tokens=retained_tokens,
            dropped_tool_result_tokens=dropped_tokens,
            token_budget=token_budget,
            token_estimate_source=token_estimate_source,
        )

    preview_count = min(preview_item_limit, dropped_count)
    lines = [f"Compacted {dropped_count} earlier tool results:"]
    for index, result in enumerate(dropped_results[:preview_count], start=1):
        lines.append(
            f"{index}. {_tool_result_preview(result, max_preview_chars=preview_char_limit)}"
        )
    remaining = dropped_count - preview_count
    if remaining > 0:
        lines.append(f"... and {remaining} more")

    return RuntimeContinuityState(
        summary_text="\n".join(lines),
        dropped_tool_result_count=dropped_count,
        retained_tool_result_count=retained_count,
        source="tool_result_window",
        original_tool_result_tokens=original_tokens,
        retained_tool_result_tokens=retained_tokens,
        dropped_tool_result_tokens=dropped_tokens,
        token_budget=token_budget,
        token_estimate_source=token_estimate_source,
    )


def _summary_anchor(
    summary_text: str | None, *, dropped_count: int, retained_count: int
) -> str | None:
    if not summary_text:
        return None
    digest = hashlib.sha256(
        f"{dropped_count}:{retained_count}:{summary_text}".encode()
    ).hexdigest()[:16]
    return f"continuity:{digest}"


def continuity_summary_metadata(
    continuity_state: RuntimeContinuityState,
) -> tuple[str | None, dict[str, int] | None]:
    summary_anchor = _summary_anchor(
        continuity_state.summary_text,
        dropped_count=continuity_state.dropped_tool_result_count,
        retained_count=continuity_state.retained_tool_result_count,
    )
    summary_source = (
        {
            "tool_result_start": 0,
            "tool_result_end": continuity_state.dropped_tool_result_count,
        }
        if summary_anchor is not None and continuity_state.source == "tool_result_window"
        else None
    )
    return summary_anchor, summary_source


def prepare_provider_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
) -> RuntimeContextWindow:
    _ = session_metadata
    effective_policy = policy or ContextWindowPolicy()
    original_count = len(tool_results)
    token_budget = _policy_token_budget(effective_policy)

    if not effective_policy.auto_compaction:
        retained_results = tool_results
        retained_count = len(retained_results)
        original_tokens = None
        retained_tokens = None
        if token_budget is not None:
            original_tokens = sum(
                _tool_result_token_estimate(
                    result,
                    tokenizer_model=effective_policy.tokenizer_model,
                ).tokens
                for result in tool_results
            )
            retained_tokens = original_tokens
        return RuntimeContextWindow(
            prompt=prompt,
            tool_results=retained_results,
            compacted=False,
            compaction_reason=None,
            original_tool_result_count=original_count,
            retained_tool_result_count=retained_count,
            max_tool_result_count=effective_policy.max_tool_results,
            original_tool_result_tokens=original_tokens,
            retained_tool_result_tokens=retained_tokens,
            dropped_tool_result_tokens=0 if token_budget is not None else None,
            token_budget=token_budget,
            token_estimate_source=(
                _token_estimate_source(effective_policy) if token_budget is not None else None
            ),
            reserved_output_tokens=effective_policy.reserved_output_tokens,
        )

    protected_recent_count = max(
        effective_policy.minimum_retained_tool_results,
        effective_policy.recent_tool_result_count,
    )
    protected_recent_count = min(protected_recent_count, original_count)
    protected_start = original_count - protected_recent_count
    truncated_results: list[ToolResult] = []
    truncated_count = 0
    for index, result in enumerate(tool_results):
        if index >= protected_start:
            content_limit = effective_policy.recent_tool_result_tokens
        else:
            content_limit = _tool_limit_for_result(result, effective_policy)
        truncated_result, was_truncated = _truncate_tool_result_content(
            result,
            limit=content_limit,
            tokenizer_model=effective_policy.tokenizer_model,
        )
        truncated_results.append(truncated_result)
        if was_truncated:
            truncated_count += 1
    prepared_results = tuple(truncated_results)

    if effective_policy.max_tool_results == 0:
        count_limited_results = (
            prepared_results[-protected_recent_count:] if protected_recent_count else ()
        )
    else:
        count_limit = max(effective_policy.max_tool_results, protected_recent_count)
        count_limited_results = prepared_results[-count_limit:]

    if token_budget is not None:
        retained_results = _retain_results_within_token_budget(
            count_limited_results,
            token_budget=token_budget,
            minimum_retained_results=protected_recent_count,
            tokenizer_model=effective_policy.tokenizer_model,
        )
    else:
        retained_results = count_limited_results

    retained_count = len(retained_results)
    compacted = retained_count < original_count
    dropped_results = tool_results[: original_count - retained_count]
    original_tokens = None
    retained_tokens = None
    dropped_tokens = None
    token_estimate_source = None
    if token_budget is not None:
        original_tokens = sum(
            _tool_result_token_estimate(
                result,
                tokenizer_model=effective_policy.tokenizer_model,
            ).tokens
            for result in prepared_results
        )
        retained_tokens = sum(
            _tool_result_token_estimate(
                result,
                tokenizer_model=effective_policy.tokenizer_model,
            ).tokens
            for result in retained_results
        )
        dropped_tokens = original_tokens - retained_tokens
        token_estimate_source = _token_estimate_source(effective_policy)

    continuity_state = (
        _build_continuity_state(
            dropped_results=dropped_results,
            retained_count=retained_count,
            preview_item_limit=effective_policy.continuity_preview_items,
            preview_char_limit=effective_policy.continuity_preview_chars,
            original_tokens=original_tokens,
            retained_tokens=retained_tokens,
            dropped_tokens=dropped_tokens,
            token_budget=token_budget,
            token_estimate_source=token_estimate_source,
        )
        if compacted
        else None
    )
    summary_anchor, summary_source = (
        continuity_summary_metadata(continuity_state)
        if continuity_state is not None
        else (None, None)
    )
    return RuntimeContextWindow(
        prompt=prompt,
        tool_results=retained_results,
        compacted=compacted,
        compaction_reason="tool_result_window" if compacted else None,
        original_tool_result_count=original_count,
        retained_tool_result_count=retained_count,
        max_tool_result_count=effective_policy.max_tool_results,
        original_tool_result_tokens=original_tokens,
        retained_tool_result_tokens=retained_tokens,
        dropped_tool_result_tokens=dropped_tokens,
        token_budget=token_budget,
        token_estimate_source=token_estimate_source,
        reserved_output_tokens=effective_policy.reserved_output_tokens,
        truncated_tool_result_count=truncated_count,
        continuity_state=continuity_state,
        summary_anchor=summary_anchor,
        summary_source=summary_source,
    )
