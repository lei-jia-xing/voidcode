from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static

from ..runtime.events import EventEnvelope
from ..runtime.session import StoredSessionSummary


class ApprovalModal(ModalScreen[Literal["allow", "deny"]]):
    CSS = """
    ApprovalModal {
        align: center middle;
    }
    #dialog {
        padding: 1 2;
        width: 80;
        height: auto;
        max-height: 80%;
        border: thick $surface;
        background: $background;
    }
    #question {
        content-align: center middle;
        width: 100%;
        margin-bottom: 1;
    }
    #payload-details {
        border: solid $accent;
        height: auto;
        max-height: 15;
        overflow-y: auto;
        margin-bottom: 1;
        padding: 0 1;
    }
    #buttons {
        height: auto;
        align: center middle;
    }
    """

    def __init__(self, event: EventEnvelope) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        tool = str(self.event.payload.get("tool", "unknown"))
        target_summary = self.event.payload.get("target_summary")
        if isinstance(target_summary, str) and target_summary:
            prompt = f"Approve {tool} for {target_summary}?"
        else:
            prompt = f"Approve {tool}?"

        with Vertical(id="dialog"):
            yield Label(prompt, id="question")

            arguments = self.event.payload.get("arguments")
            if arguments:
                import json

                try:
                    formatted_args = json.dumps(arguments, indent=2)
                    yield Static(formatted_args, id="payload-details")
                except Exception:
                    yield Static(str(arguments), id="payload-details")

            with Horizontal(id="buttons"):
                yield Button("Allow", variant="success", id="allow")
                yield Button("Deny", variant="error", id="deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("allow" if event.button.id == "allow" else "deny")


class SessionListModal(ModalScreen[str | None]):
    CSS = """
    SessionListModal {
        align: center middle;
    }
    #session-dialog {
        padding: 1 2;
        width: 80;
        height: auto;
        max-height: 30;
        border: thick $surface;
        background: $background;
    }
    """

    BINDINGS = [Binding("escape", "dismiss_modal", "Dismiss", show=False)]

    def __init__(self, sessions: tuple[StoredSessionSummary, ...]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="session-dialog"):
            yield Label("Select Session to Resume", classes="sidebar-header")
            if not self.sessions:
                yield Label("No sessions found.")
                yield OptionList("Cancel", id="session-options")
            else:
                options: list[str] = []
                for s in self.sessions:
                    short_id = s.session.id.removeprefix("session-")[:8]
                    prompt = s.prompt[:50] + ("..." if len(s.prompt) > 50 else "")
                    options.append(f"{short_id} - {prompt} [{s.status}]")
                yield OptionList(*options, id="session-options")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if str(event.option.prompt) == "Cancel" or not self.sessions:
            self.dismiss(None)
            return
        idx = event.option_index
        self.dismiss(self.sessions[idx].session.id)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class ThemePickerModal(ModalScreen[str | None]):
    CSS = """
    ThemePickerModal {
        align: center middle;
    }
    #theme-dialog {
        padding: 1 2;
        width: 60;
        height: auto;
        max-height: 20;
        border: thick $surface;
        background: $background;
    }
    """
    BINDINGS = [Binding("escape", "dismiss_modal", "Dismiss", show=False)]

    def __init__(self, themes: list[str]) -> None:
        super().__init__()
        self.themes = themes

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-dialog"):
            yield Label("Select Theme", classes="sidebar-header")
            yield OptionList(*self.themes, id="theme-options")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class ThemeModePickerModal(ModalScreen[str | None]):
    CSS = """
    ThemeModePickerModal {
        align: center middle;
    }
    #theme-mode-dialog {
        padding: 1 2;
        width: 60;
        height: auto;
        max-height: 20;
        border: thick $surface;
        background: $background;
    }
    """
    BINDINGS = [Binding("escape", "dismiss_modal", "Dismiss", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-mode-dialog"):
            yield Label("Select Theme Mode", classes="sidebar-header")
            yield OptionList("auto", "light", "dark", id="theme-mode-options")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
