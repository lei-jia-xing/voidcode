from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from ..models import TuiTimelineEvent
from .tool_activity import ToolActivityBlock


@dataclass(frozen=True, slots=True)
class TimelineRow:
    sequence: int
    event_type: str
    title: str
    summary: str = ""
    tone: str = "muted"

    @property
    def line_text(self) -> str:
        if self.summary:
            return f"#{self.sequence} {self.title} · {self.summary}"
        return f"#{self.sequence} {self.title}"


def timeline_rows_from_events(events: Iterable[TuiTimelineEvent]) -> tuple[TimelineRow, ...]:
    return tuple(timeline_row_from_event(event) for event in events)


def timeline_row_from_event(event: TuiTimelineEvent) -> TimelineRow:
    event_type = event.event_type
    if event_type == "runtime.request_received":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Request received",
            summary=_summary_from_pairs(
                ("prompt", event.payload.get("prompt")),
            ),
            tone="info",
        )
    if event_type == "runtime.skills_loaded":
        skills = event.payload.get("skills")
        summary = "skills=none"
        if isinstance(skills, list) and skills:
            typed_skills = cast(list[object], skills)
            summary = f"skills={', '.join(_compact_scalar(skill) for skill in typed_skills[:4])}"
            if len(typed_skills) > 4:
                summary = f"{summary}, ..."
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Skills loaded",
            summary=summary,
            tone="success",
        )
    if event_type == "graph.tool_request_created":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Tool requested",
            summary=_summary_from_pairs(
                ("tool", event.payload.get("tool")),
                ("arguments", event.payload.get("arguments")),
            ),
            tone="warning",
        )
    if event_type == "runtime.tool_lookup_succeeded":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Tool resolved",
            summary=_summary_from_pairs(
                ("tool", event.payload.get("tool")),
            ),
            tone="success",
        )
    if event_type == "runtime.permission_resolved":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Permission resolved",
            summary=_summary_from_pairs(
                ("tool", event.payload.get("tool")),
                ("decision", event.payload.get("decision")),
            ),
            tone="success",
        )
    if event_type == "runtime.tool_hook_pre":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Pre-hook",
            summary=_summary_from_pairs(
                ("tool_name", event.payload.get("tool_name")),
                ("status", event.payload.get("status")),
            ),
            tone="muted",
        )
    if event_type == "runtime.tool_hook_post":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Post-hook",
            summary=_summary_from_pairs(
                ("tool_name", event.payload.get("tool_name")),
                ("status", event.payload.get("status")),
            ),
            tone="muted",
        )
    if event_type == "runtime.tool_completed":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Tool completed",
            summary=_compact_payload_summary(event.payload),
            tone="success",
        )
    if event_type == "graph.response_ready":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Response ready",
            summary=_compact_payload_summary(event.payload),
            tone="success",
        )
    if event_type == "runtime.approval_requested":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Approval requested",
            summary=_summary_from_pairs(
                ("request_id", event.payload.get("request_id")),
                ("tool", event.payload.get("tool")),
                ("target", event.payload.get("target_summary")),
            ),
            tone="warning",
        )
    if event_type == "runtime.approval_resolved":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Approval resolved",
            summary=_summary_from_pairs(
                ("request_id", event.payload.get("request_id")),
                ("decision", event.payload.get("decision")),
            ),
            tone="success",
        )
    if event_type == "runtime.failed":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Runtime failed",
            summary=_summary_from_pairs(
                ("error", event.payload.get("error")),
            ),
            tone="error",
        )
    if event_type == "graph.loop_step":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Loop step",
            summary=_compact_payload_summary(event.payload),
            tone="muted",
        )
    if event_type == "graph.model_turn":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Model turn",
            summary=_compact_payload_summary(event.payload),
            tone="info",
        )
    if event_type == "runtime.memory_refreshed":
        return TimelineRow(
            sequence=event.sequence,
            event_type=event_type,
            title="Memory refreshed",
            summary=_compact_payload_summary(event.payload),
            tone="info",
        )

    return TimelineRow(
        sequence=event.sequence,
        event_type=event_type,
        title=event_type,
        summary=_compact_payload_summary(event.payload),
        tone="muted",
    )


def _summary_from_pairs(*pairs: tuple[str, object | None]) -> str:
    parts: list[str] = []
    for key, value in pairs:
        if value is None:
            continue
        compact = _compact_value(value)
        if not compact:
            continue
        parts.append(f"{key}={compact}")
    return " · ".join(parts) or "no payload"


def _compact_payload_summary(payload: dict[str, object]) -> str:
    if not payload:
        return "no payload"

    pairs = tuple((key, payload[key]) for key in sorted(payload)[:3])
    summary = _summary_from_pairs(*pairs)
    if len(payload) > 3:
        summary = f"{summary} · ..."
    return summary


def _compact_scalar(value: object) -> str:
    if isinstance(value, str):
        return _truncate(value)
    return _compact_value(value)


def _compact_value(value: object) -> str:
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, bool | int | float) or value is None:
        return json.dumps(value)
    if isinstance(value, dict):
        typed_dict = cast(dict[object, object], value)
        items = sorted((str(key), nested) for key, nested in typed_dict.items())
        parts = [f"{key}={_compact_value(nested)}" for key, nested in items[:3]]
        text = "{" + ", ".join(parts)
        if len(items) > 3:
            text = f"{text}, ..."
        return _truncate(f"{text}}}")
    if isinstance(value, list | tuple):
        typed_sequence = cast(list[object] | tuple[object, ...], value)
        parts = [_compact_value(item) for item in typed_sequence[:3]]
        text = "[" + ", ".join(parts)
        if len(typed_sequence) > 3:
            text = f"{text}, ..."
        return _truncate(f"{text}]")
    return _truncate(str(value))


def _truncate(text: str, *, limit: int = 72) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


class Timeline(Widget):
    """Main scrollable timeline of events and output."""

    DEFAULT_CSS = """
    Timeline {
        width: 100%;
        height: 1fr;
        padding: 0 1;
        background: transparent;
        border: round $panel;
        min-height: 8;
    }

    Timeline:focus-within {
        border: round $primary;
    }

    Timeline > VerticalScroll {
        width: 100%;
        height: 100%;
    }

    #timeline-rows {
        width: 100%;
        height: auto;
    }

    #timeline-content {
        color: $text;
        height: auto;
        width: 100%;
    }
    """

    def __init__(self, *, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._events: tuple[TuiTimelineEvent, ...] = ()
        self._rendered_sequences: list[int] = []
        self._current_tool_block: ToolActivityBlock | None = None
        self._current_static: Static | None = None
        self._current_static_text: Text | None = None
        self._empty_message = "No events yet.\n\nType a message to start the conversation."

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            with Vertical(id="timeline-rows"):
                yield Static(self._empty_message, id="timeline-content")

    def on_mount(self) -> None:
        self.can_focus = True

    @property
    def events(self) -> tuple[TuiTimelineEvent, ...]:
        return self._events

    def set_events(
        self,
        events: Iterable[TuiTimelineEvent],
        *,
        empty_message: str | None = None,
    ) -> None:
        self._events = tuple(events)
        if empty_message is not None:
            self._empty_message = empty_message

        new_sequences = [e.sequence for e in self._events]

        if new_sequences == self._rendered_sequences:
            return

        rows = self.query_one("#timeline-rows", Vertical)

        if (
            not new_sequences
            or new_sequences[: len(self._rendered_sequences)] != self._rendered_sequences
        ):
            self._rendered_sequences.clear()
            self._current_tool_block = None
            self._current_static = None
            self._current_static_text = None
            for child in rows.children:
                child.remove()

        if not self._events:
            if not rows.children:
                rows.mount(Static(self._empty_message, id="timeline-content"))
            self.refresh(layout=True)
            return

        for event in self._events:
            if event.sequence in self._rendered_sequences:
                continue

            if not self._rendered_sequences:
                for child in rows.children:
                    if child.id == "timeline-content":
                        child.remove()

            self._rendered_sequences.append(event.sequence)
            row = timeline_row_from_event(event)
            row_text = self._format_row(row)

            if event.event_type == "graph.tool_request_created":
                tool_name = str(event.payload.get("tool", "unknown"))
                self._current_tool_block = ToolActivityBlock(tool_name=tool_name)
                rows.mount(self._current_tool_block)
                self._current_tool_block.append_row_text(row_text)
                self._current_static = None
                self._current_static_text = None
                continue

            if self._current_tool_block is not None:
                self._current_tool_block.append_row_text(row_text)
                if event.event_type == "runtime.tool_completed":
                    self._current_tool_block.mark_completed("completed")
                    self._current_tool_block = None
                elif event.event_type == "runtime.failed":
                    self._current_tool_block.mark_completed("failed")
                    self._current_tool_block = None
                continue

            if self._current_static is None or self._current_static_text is None:
                self._current_static = Static("", id=f"timeline-row-{event.sequence}")
                self._current_static_text = Text()
                rows.mount(self._current_static)

            if len(self._current_static_text) > 0:
                self._current_static_text.append("\n")
            self._current_static_text.append_text(row_text)
            self._current_static.update(self._current_static_text)

        self.refresh(layout=True)

    def _format_row(self, row: TimelineRow) -> Text:
        text = Text()
        text.append(f"#{row.sequence} ", style="bold cyan")
        text.append(row.title, style=_tone_style(row.tone))
        if row.summary:
            text.append(" · ", style="dim")
            text.append(row.summary, style="white")
        return text


def _tone_style(tone: str) -> str:
    if tone == "success":
        return "bold green"
    if tone == "warning":
        return "bold yellow"
    if tone == "error":
        return "bold red"
    if tone == "info":
        return "bold bright_cyan"
    return "bold white"
