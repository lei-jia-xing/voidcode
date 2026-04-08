from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea


class Composer(TextArea):
    """Multiline composer with custom Enter/Shift+Enter bindings."""

    class Submitted(Message):
        """Posted when Enter is pressed and draft is not empty."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.Submitted())
            return
        elif event.key in ("shift+enter", "alt+enter", "escape enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return

        await super()._on_key(event)


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
        background: transparent;
        padding: 1;
    }

    PromptBar.minimal {
        margin-top: 0;
        padding: 0;
    }

    #prompt-status {
        width: 1fr;
        color: $text-muted;
        padding-bottom: 1;
    }

    PromptBar.minimal #prompt-status {
        display: none;
    }

    Composer {
        width: 1fr;
        height: auto;
        min-height: 3;
        max-height: 10;
        border: solid $primary;
    }

    Composer:focus {
        border: solid $primary;
    }

    PromptBar.minimal Composer, PromptBar.minimal Composer:focus {
        border: none;
    }
    """

    def __init__(
        self, *, name: str | None = None, id: str | None = None, minimal: bool = False
    ) -> None:
        super().__init__(name=name, id=id)
        self._status_text = "Idle"
        self._submit_disabled = False
        if minimal:
            self.add_class("minimal")

    def compose(self) -> ComposeResult:
        yield Static(self._status_text, id="prompt-status")
        yield Composer(id="prompt-input")

    def on_mount(self) -> None:
        self.can_focus = False
        composer = self.query_one("#prompt-input", Composer)
        composer.show_line_numbers = False

    @property
    def draft(self) -> str:
        return self.query_one("#prompt-input", Composer).text

    @property
    def submit_disabled(self) -> bool:
        return self._submit_disabled

    @property
    def status_text(self) -> str:
        return self._status_text

    def set_draft(self, value: str) -> None:
        self.query_one("#prompt-input", Composer).text = value

    def clear_draft(self) -> None:
        self.set_draft("")

    def set_submit_disabled(self, disabled: bool) -> None:
        self._submit_disabled = disabled
        input_widget = self.query_one("#prompt-input", Composer)
        input_widget.disabled = disabled

    def set_status_text(self, text: str) -> None:
        self._status_text = text
        self.query_one("#prompt-status", Static).update(text)

    @on(Composer.Submitted)
    def _on_composer_submitted(self, event: Composer.Submitted) -> None:
        event.stop()
        if not self._submit_disabled:
            self._post_submit_request(self.draft)

    def _post_submit_request(self, prompt: str) -> None:
        self.post_message(self.PromptSubmitRequested(prompt=prompt))
