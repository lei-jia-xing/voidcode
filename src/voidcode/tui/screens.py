from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from ..runtime.events import EventEnvelope


class ApprovalModal(ModalScreen[Literal["allow", "deny"]]):
    CSS = """
    ApprovalModal {
        align: center middle;
    }
    #dialog {
        padding: 1 2;
        width: 60;
        height: 11;
        border: thick $background 80%;
        background: $surface;
    }
    #question {
        content-align: center middle;
        width: 100%;
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
            with Horizontal():
                yield Button("Allow", variant="success", id="allow")
                yield Button("Deny", variant="error", id="deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("allow" if event.button.id == "allow" else "deny")
