from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from ..models import TuiApprovalRequest


class ApprovalModal(ModalScreen[bool]):
    """Modal screen for resolving runtime tool approvals."""

    DEFAULT_CSS = """
    ApprovalModal {
        align: center middle;
        background: $background 50%;
    }

    #approval-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: solid $warning;
        background: $surface;
    }

    #approval-title {
        text-style: bold;
        color: $warning;
        padding-bottom: 1;
        border-bottom: solid $panel;
        margin-bottom: 1;
    }

    #approval-content {
        margin-bottom: 2;
    }

    #approval-tool-name {
        color: $accent;
        text-style: bold;
    }

    #approval-target-summary {
        color: $text;
        padding: 1 0;
    }

    #approval-reason {
        color: $text-muted;
        text-style: italic;
    }

    #approval-buttons {
        width: 100%;
        align: right middle;
        height: auto;
    }

    Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        request: TuiApprovalRequest,
        *,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Label("Approval Required", id="approval-title")
            with Vertical(id="approval-content"):
                yield Label(f"Tool: {self.request.tool}", id="approval-tool-name")
                summary = self.request.target_summary or str(self.request.arguments)
                yield Label(f"Target: {summary}", id="approval-target-summary")
                if self.request.reason:
                    yield Label(f"Reason: {self.request.reason}", id="approval-reason")

            with Horizontal(id="approval-buttons"):
                yield Button("Reject", variant="error", id="btn-reject")
                yield Button("Approve", variant="success", id="btn-approve")

    def on_mount(self) -> None:
        self.query_one("#btn-approve", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-approve":
            self.dismiss(True)
        elif event.button.id == "btn-reject":
            self.dismiss(False)
