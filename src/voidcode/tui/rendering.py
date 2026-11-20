"""Frame rendering and transcript row rendering for the inline TUI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from voidcode.config import BannerConfig
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

from .artifact_viewer import InteractiveArtifactViewer
from .formatting import effort_segment, footer_row, json_safe, model_segment, safe, short_workspace
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
            return [styles.fg("error", "Error: Run failed")]
        if content.status == "interrupted":
            return [styles.fg("dim", "Interrupted")]
        return []
    if isinstance(content, TranscriptInterrupted):
        return []
    if isinstance(content, TranscriptChild):
        return []
    if isinstance(content, TranscriptDispatch):
        return []
    raise ValueError(f"unknown transcript content: {type(content).__name__}")


def footer_context(app: CoreTuiApp, width: int) -> str:
    """Context row from health DTO and turn usage: workspace · model · effort · context · usage."""
    if app.footer_mode == "off":
        return ""
    from voidcode.tui.local_client import CoreLocalClient

    model = app.upgrade.model if app.upgrade is not None else None
    local = isinstance(app.client, CoreLocalClient)
    workspace = short_workspace(app.upgrade.workspace, local=local) if app.upgrade is not None and app.upgrade.workspace else ""

    context_segment: str | None = None
    if app.latest_input_tokens is not None:
        used_str = f"{app.latest_input_tokens / 1000.0:.1f}k"
        window = model.context_window if model is not None else None
        if window is not None:
            context_segment = f"{used_str}/{window / 1000.0:.0f}k"
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
    """`<spinner> working` while a run or active tool is running; idle borders stay clean."""
    if not app._run_active and not app._active_tools:
        return ""
    spinner = SPINNER_FRAMES[app._spinner_tick % len(SPINNER_FRAMES)] if SPINNER_FRAMES else ""
    if app._active_tools:
        first_tool = next(iter(app._active_tools.values()))
        label = f"running {first_tool.tool_name}" if len(app._active_tools) == 1 else f"running {len(app._active_tools)} tools"
        if app._cancel_pending:
            label = f"cancelling {first_tool.tool_name}"
        else:
            label += " · Esc to interrupt"
    else:
        label = "cancelling" if app._cancel_pending else "working · Esc to interrupt"
    return f"{spinner} {label}" if spinner else label


def render_frame(app: CoreTuiApp) -> None:
    width = app.terminal.width
    if app._run_active or app._stream_dispatch is not None:
        app._spinner_tick += 1
    context = footer_context(app, width)
    context_row = (app.styles.fg("text", context) if context else "",)
    notice = app.styles.fg("dim", app.status_line) if app.status_line else ""
    quit_hint = app.styles.fg("dim", "Press Ctrl+C again to exit") if time.monotonic() - app._quit_armed_at < 1.5 else ""
    footer_rows = tuple(row for row in (*context_row, notice, quit_hint) if row)
    if app.pending_approval is not None:
        rows = app.pending_approval.render_rows(width, app.styles)
        app.terminal.paint_frame(TuiFrame(transcript_rows=(), chrome_rows=(*rows, *footer_rows), cursor=None))
    elif isinstance(app.active_overlay, InteractiveArtifactViewer):
        _render_artifact_viewer(app, width, footer_rows)
    elif app.active_overlay is not None:
        overlay = app.active_overlay
        overlay_rows = (
            overlay.render_rows(width, app.styles, max_rows=max(6, getattr(app.terminal, "height", 24) - len(footer_rows)))
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
    available = max(1, getattr(app.terminal, "height", 24) - len(footer_rows))
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
    height = getattr(app.terminal, "height", 24)
    available = max(1, height - len(footer_rows))
    app.composer.set_max_height(app.composer.input_height(available, status_visible=app.border == "borderless" and app._run_active))
    status = border_status(app)
    composer_rows = app.composer.render(status if app.border == "borderless" else "", border_status=status)
    cursor = app.composer.cursor_position
    popup = list(app.composer.completion_rows(max_rows=max(1, min(6, available - len(composer_rows)))))
    live_space = max(0, available - len(composer_rows) - len(popup))
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
    if cursor is not None and (live or popup):
        row, column = cursor
        cursor = (row + len(live) + len(popup), column)
    app.terminal.paint_frame(TuiFrame(transcript_rows=(), chrome_rows=(*live, *popup, *composer_rows, *footer_rows), cursor=cursor))
