from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static, TabbedContent, TabPane
from textual.widgets._option_list import Option

from ..runtime.events import EventEnvelope
from ..runtime.session import StoredSessionSummary

_STATUS_ICONS: dict[str, str] = {
    "completed": "●",
    "failed": "✗",
    "waiting": "◐",
    "running": "◌",
    "idle": "○",
}
_LSP_ICONS: dict[str, str] = {
    "running": "●",
    "starting": "◌",
    "stopped": "○",
    "failed": "✗",
}
_ACP_ICONS: dict[str, str] = {
    "connected": "●",
    "disconnected": "○",
    "failed": "✗",
}


class ApprovalModal(ModalScreen[Literal["allow", "deny"]]):
    CSS = """
    ApprovalModal {
        align: center middle;
    }
    #dialog {
        padding: 1 2;
        width: 70;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    #question {
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }
    .approval-detail {
        color: $text-muted;
        margin-bottom: 0;
    }
    #buttons {
        margin-top: 1;
    }
    """

    def __init__(self, event: EventEnvelope) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        payload = self.event.payload
        tool = str(payload.get("tool", "unknown"))
        target_summary = payload.get("target_summary")
        reason = payload.get("reason")
        arguments = payload.get("arguments")

        if isinstance(target_summary, str) and target_summary:
            heading = f"Approve {tool}  —  {target_summary}"
        else:
            heading = f"Approve {tool}?"

        with Vertical(id="dialog"):
            yield Label(heading, id="question")
            if isinstance(reason, str) and reason:
                yield Static(f"Reason: {reason}", classes="approval-detail")
            if isinstance(arguments, dict) and arguments:
                items = cast(dict[str, object], arguments)
                pairs = "  ".join(f"{k}={v}" for k, v in list(items.items())[:4])
                yield Static(f"Args:   {pairs}", classes="approval-detail")
            with Horizontal(id="buttons"):
                yield Button("Allow", variant="success", id="allow")
                yield Button("Deny", variant="error", id="deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("allow" if event.button.id == "allow" else "deny")


class SessionPickerScreen(ModalScreen[StoredSessionSummary | None]):
    """Telescope-style session picker.  Ctrl+P opens, Esc closes."""

    BINDINGS = [("escape", "dismiss_none", "Close")]

    CSS = """
    SessionPickerScreen {
        align: center middle;
    }
    #sp-container {
        width: 82%;
        height: 72%;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #sp-title {
        text-style: bold;
        color: $accent;
        height: 1;
        margin-bottom: 1;
    }
    #sp-list {
        height: 1fr;
    }
    #sp-empty {
        color: $text-muted;
        height: 1fr;
        content-align: center middle;
    }
    #sp-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(self, sessions: tuple[StoredSessionSummary, ...]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="sp-container"):
            yield Label("Sessions", id="sp-title")
            if not self.sessions:
                yield Label("No sessions found.", id="sp-empty")
            else:
                options: list[Option] = []
                for s in self.sessions:
                    ts = datetime.fromtimestamp(s.updated_at / 1000).strftime("%m/%d %H:%M")
                    icon = _STATUS_ICONS.get(s.status, "○")
                    sid = s.session.id[:8]
                    prompt = s.prompt[:52] + "…" if len(s.prompt) > 52 else s.prompt
                    label = f"{icon}  {ts}  [{sid}]  {prompt}"
                    options.append(Option(label, id=s.session.id))
                yield OptionList(*options, id="sp-list")
            yield Label("↑↓ navigate · enter select · esc close", id="sp-hint")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        for session in self.sessions:
            if session.session.id == option_id:
                self.dismiss(session)
                return
        self.dismiss(None)


class RuntimeDetailScreen(ModalScreen[StoredSessionSummary | None]):
    """Ctrl+x / Ctrl+P telescope modal: Sessions | Runtime tabs."""

    BINDINGS = [("escape", "dismiss_none", "Close")]

    CSS = """
    RuntimeDetailScreen {
        align: center middle;
    }
    #rd-container {
        width: 82%;
        height: 78%;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    #rd-title {
        text-style: bold;
        color: $accent;
        height: 1;
        margin-bottom: 1;
    }
    #rd-sessions-list {
        height: 1fr;
    }
    #rd-sessions-empty {
        color: $text-muted;
        height: 1fr;
        content-align: center middle;
    }
    #rd-runtime-content {
        height: 1fr;
    }
    .rd-section-label {
        color: $text-muted;
        text-style: bold;
        margin-top: 1;
        height: 1;
    }
    .rd-section-value {
        color: $text;
        min-height: 1;
        padding-left: 2;
    }
    #rd-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        sessions: tuple[StoredSessionSummary, ...],
        runtime_snapshot: dict[str, object],
        initial_tab: str = "sessions",
    ) -> None:
        super().__init__()
        self.sessions = sessions
        self.runtime_snapshot = runtime_snapshot
        self._initial_tab = initial_tab

    def compose(self) -> ComposeResult:
        with Vertical(id="rd-container"):
            yield Label("Runtime", id="rd-title")
            with TabbedContent(initial=self._initial_tab):
                with TabPane("Sessions", id="sessions"):
                    if not self.sessions:
                        yield Label("No sessions found.", id="rd-sessions-empty")
                    else:
                        options: list[Option] = []
                        for s in self.sessions:
                            ts = datetime.fromtimestamp(s.updated_at / 1000).strftime("%m/%d %H:%M")
                            icon = _STATUS_ICONS.get(s.status, "○")
                            sid = s.session.id[:8]
                            prompt = s.prompt[:52] + "…" if len(s.prompt) > 52 else s.prompt
                            label = f"{icon}  {ts}  [{sid}]  {prompt}"
                            options.append(Option(label, id=s.session.id))
                        yield OptionList(*options, id="rd-sessions-list")
                with TabPane("Runtime", id="runtime"):
                    with VerticalScroll(id="rd-runtime-content"):
                        model = str(self.runtime_snapshot.get("model") or "—")
                        engine = str(self.runtime_snapshot.get("execution_engine") or "—")
                        approval = str(self.runtime_snapshot.get("approval_mode") or "—")
                        tools_count = self.runtime_snapshot.get("tools_count")
                        yield Static("MODEL", classes="rd-section-label")
                        yield Static(f"{model}  ({engine})", classes="rd-section-value")
                        yield Static("APPROVAL", classes="rd-section-label")
                        yield Static(approval, classes="rd-section-value")
                        yield Static("TOOLS", classes="rd-section-label")
                        yield Static(
                            f"{tools_count} registered" if tools_count is not None else "—",
                            classes="rd-section-value",
                        )
                        raw_lsp = self.runtime_snapshot.get("lsp_servers")
                        lsp_entries: list[tuple[str, str]] = (
                            cast(list[tuple[str, str]], raw_lsp)
                            if isinstance(raw_lsp, list)
                            else []
                        )
                        if lsp_entries:
                            yield Static("LSP", classes="rd-section-label")
                            for name, status in lsp_entries:
                                lsp_icon = _LSP_ICONS.get(status, "○")
                                yield Static(
                                    f"{lsp_icon} {name}  {status}",
                                    classes="rd-section-value",
                                )
                        acp_status = str(self.runtime_snapshot.get("acp_status") or "—")
                        acp_icon = _ACP_ICONS.get(acp_status, "○")
                        yield Static("MCP / ACP", classes="rd-section-label")
                        yield Static(f"{acp_icon} {acp_status}", classes="rd-section-value")
            yield Label("tab switch tab · ↑↓ navigate · enter select · esc close", id="rd-hint")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option_id
        for session in self.sessions:
            if session.session.id == option_id:
                self.dismiss(session)
                return
        self.dismiss(None)
