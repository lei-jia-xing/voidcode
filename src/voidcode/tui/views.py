"""Stable facade for the TUI plugin-view renderers and interactive models.

The implementation lives in cohesive sibling modules: the closed typed outcome
contract (``view_outcomes``), pure ANSI row renderers (``view_renderers``),
selection/confirmation/approval overlays (``view_overlays``), form/ask overlays
(``view_forms``), and the finite-kind dispatcher (``view_dispatch``). This
module re-exports the public surface used by ``CoreTuiApp``, plugins, and tests.
"""

from __future__ import annotations

from .view_dispatch import UnsupportedViewError, render_plugin_view
from .view_forms import InteractiveAsk, InteractiveForm
from .view_outcomes import (
    ApprovalDecided,
    AskAnswered,
    AskCancelled,
    AskValue,
    ConfirmationDecided,
    FormValue,
    OverlayActionSelected,
    OverlayCancelled,
)
from .view_overlays import (
    OVERLAY_MAX_ROWS,
    InteractiveApproval,
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveMarkdown,
)
from .view_renderers import (
    render_active_tool,
    render_active_tools_summary,
    render_assistant_markdown,
    render_diff,
    render_fields,
    render_json,
    render_markdown,
    render_media,
    render_notice,
    render_status,
    render_table,
    render_thinking,
    render_tool_call,
    render_tool_result,
    render_user_message,
)

__all__ = [
    "OVERLAY_MAX_ROWS",
    "ApprovalDecided",
    "AskAnswered",
    "AskCancelled",
    "AskValue",
    "ConfirmationDecided",
    "FormValue",
    "InteractiveApproval",
    "InteractiveAsk",
    "InteractiveChoices",
    "InteractiveConfirmation",
    "InteractiveForm",
    "InteractiveMarkdown",
    "OverlayActionSelected",
    "OverlayCancelled",
    "UnsupportedViewError",
    "render_active_tool",
    "render_active_tools_summary",
    "render_assistant_markdown",
    "render_diff",
    "render_fields",
    "render_json",
    "render_markdown",
    "render_media",
    "render_notice",
    "render_plugin_view",
    "render_status",
    "render_table",
    "render_thinking",
    "render_tool_call",
    "render_tool_result",
    "render_user_message",
]
