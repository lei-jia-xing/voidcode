from __future__ import annotations

from .models import UICommandDefinition
from .registry import UICommandRegistry

DEFAULT_TUI_COMMANDS: tuple[UICommandDefinition, ...] = (
    UICommandDefinition("session.new", "session: new", "Start a new local session."),
    UICommandDefinition("session.resume", "session: resume", "Resume a persisted session."),
    UICommandDefinition("theme.switch", "theme: switch", "Select a TUI theme."),
    UICommandDefinition("theme.mode", "theme: mode", "Switch the TUI theme mode."),
    UICommandDefinition("view.wrap", "view: wrap", "Toggle transcript wrapping."),
    UICommandDefinition("view.sidebar", "view: sidebar", "Toggle the sidebar."),
)


def default_tui_command_registry() -> UICommandRegistry:
    return UICommandRegistry(DEFAULT_TUI_COMMANDS)
