"""Session information panels: status, context, usage, export and copy.

Every panel reads committed facts through the SDK's bounded session services
(``read_transcript``, header-only ``read_events``, ``get_usage``, ``get_budget``); nothing here writes state or
touches storage directly.
"""

from __future__ import annotations

import json

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.services import EventView, PluginServices, SessionUsageReport
from voidcode.sdk.tools import ToolFailure, ToolSuccess
from voidcode.sdk.transcript import TranscriptDispatch, TranscriptEntry, TranscriptRun, TranscriptTool

#: One panel reads at most this many events, in pages of READ_PAGE events.
MAX_EVENTS = 2000
READ_PAGE = 200
_EXPORT_CHARS = 200_000


_PANEL_DESCRIPTIONS: dict[str, str] = {
    "session_status": "Show the current session identity, event counts, usage and budget.",
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
        usage = services.get_usage()
        if self._panel == "session_status":
            events, truncated = _read_events(services)
            return _status(invocation, services, events, usage, truncated)
        if self._panel == "usage":
            return _usage(services, usage)
        entries, truncated, source_events = _read_transcript(services)
        if self._panel == "context":
            return _context(entries, usage, truncated)
        if self._panel == "export":
            return _export(entries, usage, truncated, source_events)
        if self._panel == "copy":
            if truncated:
                return CommandResult(status="error", error="transcript scan limit reached; cannot identify the last assistant response")
            return _copy(entries)
        raise ValueError(f"unknown session_info panel: {self._panel}")


def _read_events(services: PluginServices) -> tuple[tuple[EventView, ...], bool]:
    """Page the session's events up to the panel cap."""
    collected: list[EventView] = []
    after = 0
    truncated = False
    while len(collected) < MAX_EVENTS:
        page = services.read_events(after_sequence=after, limit=min(READ_PAGE, MAX_EVENTS - len(collected)), include_payload=False)
        if not page:
            break
        collected.extend(page)
        after = page[-1].sequence
        if len(page) < READ_PAGE:
            break
    else:
        truncated = True
    return tuple(collected), truncated


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


def _usage_payload(usage: SessionUsageReport) -> dict[str, object]:
    return usage.as_payload()


def _status(
    invocation: CommandInvocation,
    services: PluginServices,
    events: tuple[EventView, ...],
    usage: SessionUsageReport,
    truncated: bool,
) -> CommandResult:
    context = invocation.context
    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    budget = services.get_budget()
    last = events[-1] if events else None
    data: dict[str, object] = {
        "session_id": context.session_id,
        "run_id": context.run_id,
        "revision_id": context.revision_id,
        "actor": context.actor,
        "workspace": str(context.require_workspace()),
        "last_event_sequence": last.sequence if last is not None else 0,
        "event_counts": counts,
        "events_truncated": truncated,
        "usage": _usage_payload(usage),
        "budget": None if budget is None else budget.as_payload(),
    }
    lines = [
        f"session: {data['session_id']}",
        f"run: {data['run_id']}",
        f"revision: {data['revision_id']}",
        f"workspace: {data['workspace']}",
        f"last event: {data['last_event_sequence']}" + (" (window truncated)" if truncated else ""),
        f"turns: {usage.turn_count}, input tokens: {usage.input_tokens}, output tokens: {usage.output_tokens}, cost: {usage.total_cost_usd}",
    ]
    if budget is not None:
        lines.append(
            f"budget: input<={budget.max_input_tokens} output<={budget.max_output_tokens} "
            f"total<={budget.max_total_tokens} cost<={budget.max_cost_usd} elapsed<={budget.max_elapsed_seconds}"
        )
    lines.extend(f"{event_type}: {count}" for event_type, count in sorted(counts.items()))
    return CommandResult(status="ok", output="\n".join(lines), data=data)


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
    data = {"usage": _usage_payload(usage), "budget": None if budget is None else budget.as_payload()}
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
            block.extend(f"- tool: {call.tool_name} {json.dumps(dict(call.arguments), ensure_ascii=False)}" for call in content.tool_calls)
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
    blocks: list[tuple[str | None, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip().startswith("```"):
            language = lines[index].strip()[3:].strip() or None
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append((language, "\n".join(body).strip()))
        index += 1
    return blocks


def _result_text(result: object) -> str:
    if isinstance(result, ToolSuccess):
        output = result.output
        text = getattr(output, "text", None) or getattr(output, "presentation", None) or ""
        body = "" if result.body is None else json.dumps(result.body.as_payload(), ensure_ascii=False)
        return f"{body}\n{text}".strip()
    if isinstance(result, ToolFailure):
        return result.error
    return ""
