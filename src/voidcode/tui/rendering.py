"""Frame rendering and transcript row rendering for the inline TUI."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from voidcode.config import BannerConfig
from voidcode.sdk.client import ClientCapability, QueuedFollowup
from voidcode.sdk.icons import Icon
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.transcript import (
    TranscriptChild,
    TranscriptContent,
    TranscriptDispatch,
    TranscriptInterrupted,
    TranscriptRun,
    TranscriptTool,
)
from voidcode.sdk.views import FieldsViewData, PluginViewDescriptor, StatusViewData, TableViewData, parse_view_data

from .artifact_viewer import InteractiveArtifactViewer
from .attachments import MAX_PENDING_IMAGES, PendingImage, format_bytes
from .formatting import effort_segment, footer_row, json_safe, model_segment, prefixed_rows, safe, short_workspace
from .region import TuiFrame
from .styles import SPINNER_FRAMES, Styles
from .term import clamp_row, escape_terminal_controls, visible_width
from .views import (
    InteractiveChoices,
    InteractiveMarkdown,
    render_active_tool,
    render_active_tools_summary,
    render_assistant_markdown,
    render_thinking,
    render_tool_call,
    render_tool_result,
    render_user_message,
)

if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp

#: Second Ctrl+C inside this window quits; the footer hint, the quit check and
#: the help text all read this one value.
QUIT_ARM_SECONDS = 1.5

_BUILTIN_BANNER = (
    "███╗      ███╗",
    "╚███╗    ███╔╝",
    " ╚███╗  ███╔╝",
    "  ╚███╗███╔╝",
    "   ╚█████╔╝",
    "    ╚███╔╝",
    "     ╚═╝",
)


@dataclass
class ActiveToolProgress:
    invocation_id: str
    tool_name: str
    icon: Icon | None
    start_time: float
    session_id: str
    run_id: str
    revision_id: str
    generation: int
    lines: list[str] = field(default_factory=list)
    pending_fragment: str = ""
    last_stream: str | None = None

    def append_chunk(self, stream_name: str, chunk: str, *, cols: int, rows: int) -> None:
        """Incrementally append a text chunk, respecting stream changes and viewport capacity."""
        if self.last_stream is not None and self.last_stream != stream_name and self.pending_fragment:
            self.lines.append(self.pending_fragment)
            self.pending_fragment = ""
        self.last_stream = stream_name
        text = self.pending_fragment + chunk
        parts = text.split("\n")
        cell_capacity = max(cols, cols * rows)
        for line in parts[:-1]:
            cleaned = line[:-1] if line.endswith("\r") else line
            if len(cleaned) > cell_capacity:
                cleaned = cleaned[-cell_capacity:]
            self.lines.append(cleaned)
        last = parts[-1]
        if len(last) > cell_capacity:
            last = last[-cell_capacity:]
        self.pending_fragment = last
        max_lines = max(1, rows)
        if len(self.lines) > max_lines:
            self.lines = self.lines[-max_lines:]

    def visible_tail(self, count: int) -> list[str]:
        """Return the last `count` lines including any pending partial line."""
        if count <= 0:
            return []
        items = list(self.lines)
        if self.pending_fragment:
            items.append(self.pending_fragment)
        return items[-count:]


def banner_rows(banner: BannerConfig | None, styles: Styles, *, width: int) -> list[str]:
    """Centered welcome mark rows, or nothing when disabled/widthless."""
    if banner is None or width <= 0:
        return []
    raw = _BUILTIN_BANNER if banner.text is None else tuple(banner.text.split("\n"))
    cleaned = [escape_terminal_controls(line.rstrip(), preserve_tabs=False) for line in raw]
    if not any(line.strip() for line in cleaned):
        return []
    colors = banner.colors
    content_width = min(width, max(visible_width(line) for line in cleaned))
    pad = " " * ((width - content_width) // 2)
    rows: list[str] = []
    for index, line in enumerate(cleaned):
        clipped = clamp_row(line, width)
        color = colors[index * (len(colors) - 1) // max(1, len(cleaned) - 1)]
        rows.append(pad + styles.fg("accent", clipped, color=color))
    return rows


def content_rows(
    content: TranscriptContent,
    width: int,
    styles: Styles,
    *,
    include_tool_header: bool = True,
) -> list[str]:
    """Render one typed transcript content into settled rows."""
    if isinstance(content, ProviderMessage):
        if content.role == "user":
            rows: list[str] = []
            if content.images:
                badges = " ".join(styles.fg("accent", f"[Image: {img.media_type}]") for img in content.images)
                rows.append(badges)
            if content.content and content.content.strip():
                rows.extend(render_user_message(content.content, width, styles))
            return rows
        if content.role == "system":
            return []
        rows = []
        if content.content:
            rows.extend(render_assistant_markdown(content.content, width, styles))
        return rows
    if isinstance(content, ProviderTurnResult):
        rows = []
        if isinstance(content.reasoning, str) and content.reasoning:
            rows.extend(render_thinking(content.reasoning, width, styles))
        if isinstance(content.output, str) and content.output:
            rows.extend(render_assistant_markdown(content.output, width, styles))
        return rows
    if isinstance(content, TranscriptTool):
        rows = []
        if include_tool_header:
            rows.extend(
                render_tool_call(
                    safe(content.call.tool_name or "tool"),
                    json_safe(content.call.arguments),
                    width,
                    styles,
                    icon=content.icon,
                )
            )
        rows.extend(render_tool_result(content.result, width, styles, interrupted=content.interrupted))
        return rows
    if isinstance(content, TranscriptRun):
        if content.status == "failed":
            return run_notice_rows("failed", content.error, styles)
        if content.status == "interrupted":
            return run_notice_rows("interrupted", content.reason, styles, stop_cause=content.stop_cause)
        return []
    if isinstance(content, TranscriptInterrupted):
        return []
    if isinstance(content, TranscriptChild):
        return []
    if isinstance(content, TranscriptDispatch):
        return []
    raise ValueError(f"unknown transcript content: {type(content).__name__}")


def run_notice_rows(status: str, detail: object, styles: Styles, *, stop_cause: object = None) -> list[str]:
    """Render terminal run notices consistently across live events and transcript replay."""
    if status == "failed":
        return prefixed_rows("Error:", str(detail or "Run failed"), styles, slot="error")
    if stop_cause in ("user_stop", "owner_cancel"):
        return [styles.fg("dim", "Operation aborted")]
    if detail:
        label = "Stopped:" if stop_cause == "limit_reached" else "Interrupted:"
        return prefixed_rows(label, str(detail), styles, slot="dim")
    return [styles.fg("dim", "Interrupted")]


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m{secs:02d}s"
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours}h{minutes:02d}m"


def queued_followup_rows(followups: tuple[QueuedFollowup, ...], *, width: int, styles: Styles) -> list[str]:
    """Render server-queued user follow-ups directly above the composer."""
    if not followups or width <= 0:
        return []
    visible = followups[:2]
    rows: list[str] = []
    for index, item in enumerate(visible):
        preview = escape_terminal_controls(" ".join(item.prompt.split()), preserve_tabs=False)
        suffix = f" ({item.image_count} img)" if item.image_count == 1 else f" ({item.image_count} imgs)" if item.image_count > 1 else ""
        hint = ""
        if index == len(visible) - 1:
            extra = len(followups) - len(visible)
            more = f" (+{extra} more)" if extra > 0 else ""
            hint = f"{more} · Alt+Up edit"
        prefix_text = "↳ Queued: " if len(followups) == 1 else f"↳ Queued #{index + 1}: "
        available = max(1, width - visible_width(prefix_text) - visible_width(suffix) - visible_width(hint))
        shown_preview = preview if visible_width(preview) <= available else clamp_row(preview, max(1, available - 1)) + "…"
        rows.append(
            styles.fg("accent", prefix_text)
            + styles.fg("text", shown_preview)
            + (styles.fg("dim", suffix) if suffix else "")
            + (styles.fg("dim", hint) if hint else "")
        )
    return [clamp_row(row, width) for row in rows]


#: Typed path entry lives in one composer-adjacent row; the draft stays untouched.
ATTACHMENT_PATH_PREFIX = "Image path: "


def pending_attachment_rows(images: Sequence[PendingImage], *, width: int, styles: Styles) -> list[str]:
    """Render pending composer images with their one visible removal path."""
    if not images or width <= 0:
        return []
    rows: list[str] = []
    for index, image in enumerate(images):
        label = escape_terminal_controls(image.label, preserve_tabs=False)
        meta = f"{image.media_type}, {format_bytes(image.bytes)}"
        suffix = f" · {len(images)}/{MAX_PENDING_IMAGES}"
        if index == len(images) - 1:
            suffix += f" · Alt+R remove last · /tui detach {index + 1}"
        prefix = f"[{index + 1}] "
        available = max(1, width - visible_width(prefix) - visible_width(meta) - visible_width(suffix) - 3)
        shown = label if visible_width(label) <= available else clamp_row(label, max(1, available - 1)) + "…"
        row = styles.fg("accent", prefix) + styles.fg("text", shown) + styles.fg("dim", f" ({meta}){suffix}")
        rows.append(clamp_row(row, width))
    return rows


def attachment_path_rows(prompt: str, *, width: int, styles: Styles) -> list[str]:
    """One typed path-entry row; failures and the draft-preservation promise stay visible."""
    if width <= 0:
        return []
    typed = escape_terminal_controls(prompt, preserve_tabs=False)
    hint = " Enter attach · Esc cancel · draft kept"
    available = max(1, width - visible_width(ATTACHMENT_PATH_PREFIX) - visible_width(hint))
    shown = typed if visible_width(typed) <= available else clamp_row(typed, max(1, available - 1)) + "…"
    row = styles.fg("accent", ATTACHMENT_PATH_PREFIX) + styles.fg("text", shown) + styles.fg("dim", hint)
    return [clamp_row(row, width)]


def attachment_path_cursor(prompt: str, width: int) -> int:
    """Native cursor column at the typed path's end, bounded by the terminal width."""
    typed = escape_terminal_controls(prompt, preserve_tabs=False)
    return min(max(0, width - 1), visible_width(ATTACHMENT_PATH_PREFIX) + visible_width(typed))


def live_view_rows(views: tuple[PluginViewDescriptor, ...], *, width: int, max_rows: int, styles: Styles) -> list[str]:
    """Render live-enabled declarative plugin views above the composer."""
    if not views or width <= 0 or max_rows <= 0:
        return []
    rows: list[str] = []
    for view in views:
        if not view.live or len(rows) >= max_rows:
            continue
        parsed = parse_view_data(view.view_type, view.data)
        action_hint = f" · {safe(view.live_action.label)}" if view.live_action is not None else ""
        header = clamp_row(f"◈ {safe(view.title)}{action_hint}", width)
        rows.append(styles.fg("accent", header))
        if isinstance(parsed, TableViewData):
            remaining_rows = max_rows - len(rows)
            if remaining_rows <= 0:
                continue
            shown_count = remaining_rows if len(parsed.rows) <= remaining_rows else max(0, remaining_rows - 1)
            for cells in parsed.rows[:shown_count]:
                text = " · ".join(safe(cell) for cell in cells if cell)
                lower = text.lower()
                tone = "dim"
                if "approval" in lower:
                    tone = "warning"
                elif "failed" in lower or "interrupted" in lower:
                    tone = "error"
                elif "running" in lower or "queued" in lower:
                    tone = "accent"
                rows.append(styles.fg(tone, clamp_row(f"  {text}", width)))
            hidden = len(parsed.rows) - shown_count
            if hidden > 0 and len(rows) < max_rows:
                rows.append(styles.fg("dim", clamp_row(f"  +{hidden} more", width)))
        elif isinstance(parsed, StatusViewData) and len(rows) < max_rows:
            rows.append(styles.fg("text", clamp_row(f"  {safe(parsed.label)}: {safe(parsed.value)}", width)))
        elif isinstance(parsed, FieldsViewData):
            for field_row in parsed.rows:
                if len(rows) >= max_rows:
                    break
                rows.append(styles.fg("text", clamp_row(f"  {safe(field_row.label)}: {safe(field_row.value)}", width)))
    return rows[:max_rows]


def footer_context(app: CoreTuiApp, width: int) -> str:
    """Context row from health DTO and turn usage: workspace · model · effort · context · usage."""
    if app.footer_mode == "off":
        return ""
    model = app.upgrade.model if app.upgrade is not None else None
    local = app.client.capabilities.supports(ClientCapability.EMBEDDED)
    workspace = short_workspace(app.upgrade.workspace, local=local) if app.upgrade is not None and app.upgrade.workspace else ""

    context_segment: str | None = None
    if app.latest_input_tokens is not None:
        used_str = f"{app.latest_input_tokens / 1000.0:.1f}k"
        window = model.context_window if model is not None else None
        if window is not None:
            context_text = f"{used_str}/{window / 1000.0:.0f}k"
            ratio = app.latest_input_tokens / window if window > 0 else 0.0
            if ratio >= 0.95:
                context_segment = app.styles.fg("error", context_text)
            elif ratio >= 0.80:
                context_segment = app.styles.fg("warning", context_text)
            else:
                context_segment = context_text
        else:
            context_segment = f"{used_str} ctx"

    usage_segment: str | None = None
    total = app.cumulative_input_tokens + app.cumulative_output_tokens
    if total > 0:
        usage_segment = f"{total / 1000.0:.1f}k total"
    return footer_row(
        workspace,
        model_segment(model),
        effort_segment(model),
        context_segment,
        usage_segment,
        width=width,
        compact=app.footer_mode == "compact",
    )


def border_status(app: CoreTuiApp) -> str:
    """`<spinner> working/running` with elapsed time and active tool names; idle borders stay clean."""
    if not app._run_active and not app._active_tools:
        return ""
    spinner = SPINNER_FRAMES[app._spinner_tick % len(SPINNER_FRAMES)] if SPINNER_FRAMES else ""
    parts: list[str] = []
    if app._active_tools:
        names = [tool.tool_name for tool in app._active_tools.values()]
        shown = ", ".join(names[:3])
        if len(names) > 3:
            shown = f"{shown} +{len(names) - 3}"
        verb = "cancelling" if app._cancel_pending else "running"
        parts.append(f"{verb} {shown}")
    else:
        parts.append("cancelling" if app._cancel_pending else "working")
    if app._run_started_at is not None:
        parts.append(_format_elapsed(time.monotonic() - app._run_started_at))
    if not app._cancel_pending:
        parts.append("Esc to interrupt")
    label = " · ".join(parts)
    return f"{spinner} {label}" if spinner else label


def render_frame(app: CoreTuiApp) -> None:
    # Layout to the widest row paint_frame accepts, not the physical width:
    # when the terminal's autowrap state is unknown the writer reserves the
    # last cell, and a row laid out to the physical width would lose its tail.
    width = app.terminal.content_width
    if app._run_active or app._stream_dispatch is not None:
        app._spinner_tick += 1
    context = footer_context(app, width)
    context_row = (app.styles.fg("text", context) if context else "",)
    notice = app.styles.fg("dim", app.status_line) if app.status_line else ""
    quit_hint = app.styles.fg("dim", "Press Ctrl+C again to exit") if time.monotonic() - app._quit_armed_at < QUIT_ARM_SECONDS else ""
    footer_rows = tuple(row for row in (*context_row, notice, quit_hint) if row)
    if app.pending_approval is not None:
        rows = app.pending_approval.render_rows(width, app.styles)
        app.terminal.paint_frame(TuiFrame(transcript_rows=(), chrome_rows=(*rows, *footer_rows), cursor=None))
    elif isinstance(app.active_overlay, InteractiveArtifactViewer):
        _render_artifact_viewer(app, width, footer_rows)
    elif app.active_overlay is not None:
        overlay = app.active_overlay
        overlay_rows = (
            overlay.render_rows(width, app.styles, max_rows=max(6, app.terminal.height - len(footer_rows)))
            if isinstance(overlay, (InteractiveMarkdown, InteractiveChoices))
            else overlay.render_rows(width, app.styles)
        )
        app.terminal.paint_frame(TuiFrame(transcript_rows=(), chrome_rows=(*overlay_rows, *footer_rows), cursor=None))
    else:
        _render_composer(app, width, footer_rows)


def _render_artifact_viewer(app: CoreTuiApp, width: int, footer_rows: tuple[str, ...]) -> None:
    viewer = app.active_overlay
    assert isinstance(viewer, InteractiveArtifactViewer)
    deadline = app._artifact_ack_deadline
    if (
        deadline is not None
        and deadline[:2] == (viewer.identity, viewer.source.sha256)
        and viewer.image_status == "transferring"
        and time.monotonic() >= deadline[2]
    ):
        viewer.fail_image(viewer.source.sha256, "Kitty upload acknowledgement timed out; no terminal image success was observed.")
        app._artifact_ack_deadline = None
    available = max(1, app.terminal.height - len(footer_rows))
    rows = viewer.render_rows(width, app.styles, max_rows=available)
    placement_id = app.terminal.kitty_placement_id(viewer.source.sha256)
    placement = (
        viewer.image_placement(
            max_columns=width,
            max_rows=max(0, available - viewer.image_row),
            cell_pixels=app.terminal.cell_pixel_size,
            placement_id=placement_id,
        )
        if placement_id is not None
        else None
    )
    app.terminal.paint_frame(TuiFrame(chrome_rows=(*rows, *footer_rows), image=placement))


def _render_composer(app: CoreTuiApp, width: int, footer_rows: tuple[str, ...]) -> None:
    app.composer.set_width(width)
    height = app.terminal.height
    available = max(1, height - len(footer_rows))
    app.composer.set_max_height(app.composer.input_height(available, status_visible=app.border == "borderless" and app._run_active))
    status = border_status(app)
    composer_rows = app.composer.render(status if app.border == "borderless" else "", border_status=status)
    cursor = app.composer.cursor_position
    popup = list(app.composer.completion_rows(max_rows=max(1, min(6, available - len(composer_rows)))))
    queued = queued_followup_rows(app._queued_followups, width=width, styles=app.styles) if available > len(composer_rows) else []
    pending = pending_attachment_rows(app._pending_images.items, width=width, styles=app.styles)
    path_rows = attachment_path_rows(app._attachment_path, width=width, styles=app.styles) if app._attachment_path is not None else []
    remaining_live = max(0, available - len(composer_rows) - len(popup) - len(queued) - len(pending) - len(path_rows))
    has_other_live = bool(app._active_tools or app._stream_dispatch is not None)
    view_budget = remaining_live if not has_other_live else max(0, remaining_live - 1)
    view_rows = live_view_rows(app._live_views, width=width, max_rows=view_budget, styles=app.styles)
    live_space = max(0, remaining_live - len(view_rows))
    live: list[str] = []
    if live_space > 0:
        tools = list(app._active_tools.values())
        num_tools = len(tools)
        if num_tools > 0:
            spinner = SPINNER_FRAMES[app._spinner_tick % len(SPINNER_FRAMES)] if SPINNER_FRAMES else ""
            if num_tools > live_space:
                tool_pairs = [(t.tool_name, max(0.0, time.monotonic() - t.start_time)) for t in tools]
                live.extend(render_active_tools_summary(tool_pairs, width, app.styles, spinner=spinner))
                remaining = max(0, live_space - len(live))
                for t in tools[-remaining:] if remaining > 0 else []:
                    live.extend(
                        render_active_tool(
                            t.tool_name,
                            max(0.0, time.monotonic() - t.start_time),
                            [],
                            width,
                            app.styles,
                            icon=t.icon,
                            spinner=spinner,
                            max_tail_rows=0,
                        )
                    )
            else:
                remaining_tail_space = live_space - num_tools
                for index, t in enumerate(tools):
                    is_latest = index == num_tools - 1
                    tail_budget = remaining_tail_space if is_latest else 0
                    live_tail = t.visible_tail(tail_budget) if tail_budget > 0 else []
                    live.extend(
                        render_active_tool(
                            t.tool_name,
                            max(0.0, time.monotonic() - t.start_time),
                            live_tail,
                            width,
                            app.styles,
                            icon=t.icon,
                            spinner=spinner,
                            max_tail_rows=tail_budget,
                        )
                    )
        else:
            stream_rows = app._live_stream_rows(width)
            live = stream_rows[-live_space:] if stream_rows else []
    combined_live = [*view_rows, *live]
    if app._attachment_path is not None:
        row = len(combined_live) + len(popup) + len(queued) + len(pending)
        cursor = (row, attachment_path_cursor(app._attachment_path, width))
    elif cursor is not None and (combined_live or popup or queued or pending):
        row, column = cursor
        cursor = (row + len(combined_live) + len(popup) + len(queued) + len(pending), column)
    app.terminal.paint_frame(
        TuiFrame(
            transcript_rows=(),
            chrome_rows=(*combined_live, *popup, *queued, *pending, *path_rows, *composer_rows, *footer_rows),
            cursor=cursor,
        )
    )
