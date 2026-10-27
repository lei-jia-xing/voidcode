"""Session information panels: context, usage, export and copy."""

from __future__ import annotations

import json

from markdown_it import MarkdownIt

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.services import PluginServices, SessionUsageReport
from voidcode.sdk.tools import TextOutput, ToolResult, ToolSuccess
from voidcode.sdk.transcript import TranscriptDispatch, TranscriptEntry, TranscriptRun, TranscriptTool

#: One panel reads at most this many events, in pages of READ_PAGE events.
MAX_EVENTS = 2000
READ_PAGE = 200
_EXPORT_CHARS = 200_000


_PANEL_DESCRIPTIONS: dict[str, str] = {
    "context": "Show what the model context is made of, with measured and estimated sizes.",
    "usage": "Show measured token usage, cost and the active budget for this session.",
    "export": "Export the session transcript as Markdown.",
    "copy": "List copyable blocks from the last assistant response.",
}


class SessionInfoCommand:
    """One read-only panel over the calling session's committed facts."""

    def __init__(self, panel: str) -> None:
        self.definition = CommandDefinition(
            name=panel,
            description=_PANEL_DESCRIPTIONS[panel],
            argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )
        self._panel = panel

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        usage = services.get_usage()
        if self._panel == "usage":
            return _usage(services, usage)
        entries, truncated, source_events = _read_transcript(services)
        if self._panel == "context":
            if truncated:
                return CommandResult(status="error", error="transcript scan limit reached; cannot identify the latest dispatched context")
            return _context(entries, usage, truncated)
        if self._panel == "export":
            return _export(entries, usage, truncated, source_events)
        if self._panel == "copy":
            if truncated:
                return CommandResult(status="error", error="transcript scan limit reached; cannot identify the last assistant response")
            return _copy(entries)
        raise ValueError(f"unknown session_info panel: {self._panel}")


def _read_transcript(services: PluginServices) -> tuple[tuple[TranscriptEntry, ...], bool, int]:
    entries: list[TranscriptEntry] = []
    cursor = 0
    scanned = 0
    while scanned < MAX_EVENTS:
        page = services.read_transcript(after_sequence=cursor, limit=min(READ_PAGE, MAX_EVENTS - scanned))
        entries.extend(page.entries)
        cursor = page.next_sequence
        scanned += page.source_events
        if not page.has_more:
            return tuple(entries), False, scanned
    return tuple(entries), True, scanned


def _context(entries: tuple[TranscriptEntry, ...], usage: SessionUsageReport, truncated: bool) -> CommandResult:
    window = next(((entry.sequence, entry.content.window) for entry in reversed(entries) if isinstance(entry.content, TranscriptDispatch)), None)
    data: dict[str, object] = {
        "events_window_truncated": truncated,
        "measured_input_tokens": usage.input_tokens,
        "measured_cache_read_tokens": usage.cache_read_tokens,
        "dispatch_sequence": None,
        "message_characters": None,
        "tool_schema_characters": None,
        "tool_count": None,
        "total_characters": None,
        "segments": [],
    }
    lines = [f"measured input tokens: {usage.input_tokens} (cache read: {usage.cache_read_tokens})"]
    if window is None:
        lines.append("no dispatched provider request has been measured for this session yet")
        return CommandResult(status="ok", output="\n".join(lines), data=data)

    sequence, measured = window
    data.update(
        {
            "dispatch_sequence": sequence,
            "message_characters": measured.message_characters,
            "tool_schema_characters": measured.tool_schema_characters,
            "tool_count": measured.tool_count,
            "total_characters": measured.total_characters,
            "segments": [segment.as_payload() for segment in measured.segments],
        }
    )
    lines.append(
        f"last dispatch {sequence}: messages {measured.message_characters} chars in {len(measured.segments)} segment(s), "
        f"tool schema {measured.tool_schema_characters} chars (core-side neutral encoding) for {measured.tool_count} tool(s)"
    )
    for index, segment in enumerate(measured.segments):
        detail = f"  [{index}] {segment.role}"
        if segment.tool_name:
            detail += f" tool={segment.tool_name}"
        if segment.tool_calls:
            detail += f" tool_calls={segment.tool_calls}"
        if segment.reasoning_characters:
            detail += f" reasoning={segment.reasoning_characters} chars"
        lines.append(f"{detail}: {segment.characters} chars")
    return CommandResult(status="ok", output="\n".join(lines), data=data)


def _usage(services: PluginServices, usage: SessionUsageReport) -> CommandResult:
    budget = services.get_budget()
    data = {"usage": usage.as_payload(), "budget": None if budget is None else budget.as_payload()}
    lines = [
        f"turns: {usage.turn_count}",
        f"input tokens: {usage.input_tokens} (uncached: {usage.uncached_input_tokens}, cache read: {usage.cache_read_tokens})",
        f"output tokens: {usage.output_tokens}",
        f"cost usd: {usage.total_cost_usd}",
        f"elapsed seconds: {usage.elapsed_seconds}",
        f"unknown usage present: {usage.has_unknown_usage}",
    ]
    if budget is not None:
        lines.append(f"budget: {budget.as_payload()}")
    return CommandResult(status="ok", output="\n".join(lines), data=data)


def _export(entries: tuple[TranscriptEntry, ...], usage: SessionUsageReport, truncated: bool, source_events: int) -> CommandResult:
    lines = [
        "# Session export",
        "",
        f"- events: {source_events}" + (" (window truncated)" if truncated else ""),
        f"- turns: {usage.turn_count}",
        f"- input tokens: {usage.input_tokens}, output tokens: {usage.output_tokens}, cost usd: {usage.total_cost_usd}",
        "",
    ]
    for entry in entries:
        lines.extend(_export_entry(entry))
    markdown = "\n".join(lines)
    if len(markdown) > _EXPORT_CHARS:
        markdown = markdown[:_EXPORT_CHARS] + "\n\n[export truncated]"
    return CommandResult(status="ok", output=markdown, data={"events": source_events, "truncated": truncated or len(markdown) > _EXPORT_CHARS})


def _export_entry(entry: TranscriptEntry) -> list[str]:
    content = entry.content
    if isinstance(content, ProviderMessage):
        return [f"## {content.role}", "", content.content or "", ""]
    if isinstance(content, ProviderTurnResult):
        block = [f"## assistant ({entry.sequence})", "", content.output or ""]
        if content.tool_calls:
            block.append("")
            block.extend(
                f"- tool: {call.tool_name} {json.dumps(json_wire_object(call.arguments), ensure_ascii=False)}" for call in content.tool_calls
            )
        return [*block, ""]
    if isinstance(content, TranscriptTool):
        status = "interrupted" if content.interrupted else content.result.status
        return [f"### tool {content.call.tool_name} [{status}]", "", _result_text(content.result), ""]
    if isinstance(content, TranscriptRun):
        return [f"> run {content.status} at {entry.sequence}", ""]
    return []


def _copy(entries: tuple[TranscriptEntry, ...]) -> CommandResult:
    last = next((entry.content for entry in reversed(entries) if isinstance(entry.content, ProviderTurnResult)), None)
    if last is None:
        return CommandResult(status="error", error="no assistant response to copy from")
    text = last.output or ""
    blocks = _fenced_blocks(text)
    if not blocks:
        return CommandResult(status="ok", output=text, data={"blocks": [{"label": "last assistant response", "text": text}]})
    data = {"blocks": [{"label": f"code block {index + 1} ({language or 'text'})", "text": block} for index, (language, block) in enumerate(blocks)]}
    rendered = "\n".join(f"- {item['label']}" for item in data["blocks"])
    return CommandResult(status="ok", output=rendered, data=data)


def _fenced_blocks(text: str) -> list[tuple[str | None, str]]:
    return [(token.info.strip() or None, token.content) for token in MarkdownIt("commonmark").parse(text) if token.type == "fence"]


def _result_text(result: ToolResult) -> str:
    if isinstance(result, ToolSuccess):
        output = result.output
        text = (output.text if isinstance(output, TextOutput) else None) or output.presentation or ""
        body = "" if result.body is None else json.dumps(json_wire_object(result.body.as_payload()), ensure_ascii=False)
        return f"{body}\n{text}".strip()
    return result.error
