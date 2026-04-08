from __future__ import annotations

from typing import Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Collapsible, Static


class ToolActivityBlock(Collapsible):
    """Grouped block of tool activity events."""

    DEFAULT_CSS = """
    ToolActivityBlock {
        margin: 1 0;
        background: $boost;
        border-left: vkey $accent;
    }

    ToolActivityBlock.-failed {
        border-left: vkey $error;
    }

    ToolActivityBlock.-completed {
        border-left: vkey $success;
        opacity: 0.8;
    }

    #tool-events-content {
        padding: 0 1;
        height: auto;
    }
    """

    def __init__(
        self,
        tool_name: str,
        *,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(title=f"🛠️  {tool_name}", name=name, id=id)
        self.tool_name = tool_name
        self.rows: list[Text] = []
        self.status: Literal["active", "completed", "failed"] = "active"
        self.add_class("-active")

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-events-content")

    def append_row_text(self, text: Text) -> None:
        self.rows.append(text)
        self._refresh()

    def mark_completed(self, status: Literal["completed", "failed"]) -> None:
        self.status = status
        self.collapsed = True
        self.remove_class("-active", "-completed", "-failed")
        self.add_class(f"-{status}")
        self.title = f"🛠️  {self.tool_name} [{status}]"

    def _refresh(self) -> None:
        doc = Text()
        for i, text in enumerate(self.rows):
            if i > 0:
                doc.append("\n")
            doc.append(text)
        try:
            static = self.query_one("#tool-events-content", Static)
            static.update(doc)
        except Exception:
            pass

    def on_mount(self) -> None:
        self._refresh()
