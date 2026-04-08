from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Static


class PromptBar(Widget):
    """Input bar at the bottom for prompts."""

    class PromptSubmitRequested(Message):
        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

    DEFAULT_CSS = """
    PromptBar {
        width: 100%;
        height: auto;
        dock: bottom;
        margin-top: 1;
        background: $panel;
        padding: 1;
    }

    #prompt-status {
        width: 1fr;
        color: $text-muted;
        padding-bottom: 1;
    }

    #prompt-controls {
        width: 100%;
        height: 3;
    }

    #prompt-controls > Input {
        width: 1fr;
        height: 100%;
        border: solid $primary;
        margin-right: 1;
    }

    #prompt-controls > Input:focus {
        border: double $accent;
    }

    #prompt-submit {
        width: 12;
        min-width: 12;
    }
    """

    def __init__(self, *, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._status_text = "Idle"
        self._submit_disabled = False

    def compose(self) -> ComposeResult:
        yield Static(self._status_text, id="prompt-status")
        with Horizontal(id="prompt-controls"):
            yield Input(placeholder="Type a command or prompt...", id="prompt-input")
            yield Button("Submit", id="prompt-submit")

    def on_mount(self) -> None:
        self.can_focus = False

    @property
    def draft(self) -> str:
        return self.query_one("#prompt-input", Input).value

    @property
    def submit_disabled(self) -> bool:
        return self._submit_disabled

    @property
    def status_text(self) -> str:
        return self._status_text

    def set_draft(self, value: str) -> None:
        self.query_one("#prompt-input", Input).value = value

    def clear_draft(self) -> None:
        self.set_draft("")

    def set_submit_disabled(self, disabled: bool) -> None:
        self._submit_disabled = disabled
        input_widget = self.query_one("#prompt-input", Input)
        button = self.query_one("#prompt-submit", Button)
        input_widget.disabled = disabled
        button.disabled = disabled

    def set_status_text(self, text: str) -> None:
        self._status_text = text
        self.query_one("#prompt-status", Static).update(text)

    @on(Input.Submitted, "#prompt-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._post_submit_request(event.value)

    @on(Button.Pressed, "#prompt-submit")
    def _on_submit_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._post_submit_request(self.draft)

    def _post_submit_request(self, prompt: str) -> None:
        self.post_message(self.PromptSubmitRequested(prompt=prompt))
