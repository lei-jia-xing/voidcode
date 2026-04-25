from __future__ import annotations

from dataclasses import dataclass

from ..tools.contracts import ToolResult


@dataclass(frozen=True, slots=True)
class RuntimeContinuityState:
    summary_text: str | None = None
    dropped_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    source: str = "tool_result_window"
    # Lightweight versioning for continuity state to aid reinjection/refresh
    # semantics. This is incremented when the shape evolves and is included
    # in the serialized payload so consumers can decide how to handle newer
    # fields.
    version: int = 1

    def metadata_payload(self) -> dict[str, object]:
        return {
            "summary_text": self.summary_text,
            "dropped_tool_result_count": self.dropped_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "source": self.source,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    max_tool_results: int = 4
    continuity_preview_items: int = 3
    continuity_preview_chars: int = 80

    def __post_init__(self) -> None:
        if self.max_tool_results < 0:
            raise ValueError("max_tool_results must be >= 0")
        if self.continuity_preview_items < 1:
            raise ValueError("continuity_preview_items must be >= 1")
        if self.continuity_preview_chars < 1:
            raise ValueError("continuity_preview_chars must be >= 1")


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    max_tool_result_count: int = 0
    continuity_state: RuntimeContinuityState | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "compacted": self.compacted,
            "compaction_reason": self.compaction_reason,
            "original_tool_result_count": self.original_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "max_tool_result_count": self.max_tool_result_count,
        }
        if self.continuity_state is not None:
            payload["continuity_state"] = self.continuity_state.metadata_payload()
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
) -> RuntimeContinuityState:
    dropped_count = len(dropped_results)
    if dropped_count == 0:
        return RuntimeContinuityState(retained_tool_result_count=retained_count)

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
    )


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

    if effective_policy.max_tool_results == 0:
        retained_results: tuple[ToolResult, ...] = ()
    else:
        retained_results = tool_results[-effective_policy.max_tool_results :]

    retained_count = len(retained_results)
    compacted = retained_count < original_count
    dropped_results = tool_results[: original_count - retained_count]

    return RuntimeContextWindow(
        prompt=prompt,
        tool_results=retained_results,
        compacted=compacted,
        compaction_reason="tool_result_window" if compacted else None,
        original_tool_result_count=original_count,
        retained_tool_result_count=retained_count,
        max_tool_result_count=effective_policy.max_tool_results,
        continuity_state=(
            _build_continuity_state(
                dropped_results=dropped_results,
                retained_count=retained_count,
                preview_item_limit=effective_policy.continuity_preview_items,
                preview_char_limit=effective_policy.continuity_preview_chars,
            )
            if compacted
            else None
        ),
    )
