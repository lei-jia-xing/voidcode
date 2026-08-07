from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..runtime.events import EventEnvelope
from ..runtime.question import QuestionResponse
from ..runtime.session import StoredSessionSummary


class ApprovalModal(ModalScreen[Literal["allow", "deny"]]):
    BINDINGS = [
        Binding("y", "allow", "Allow", show=True),
        Binding("n", "deny", "Deny", show=True),
        Binding("escape", "deny", "Deny", show=False),
    ]

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

            reason = self.event.payload.get("reason")
            if isinstance(reason, str) and reason:
                yield Static(f"Why: {reason}", classes="approval-reason")

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

    def action_allow(self) -> None:
        self.dismiss("allow")

    def action_deny(self) -> None:
        self.dismiss("deny")


@dataclass(frozen=True, slots=True)
class _QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class _QuestionPage:
    header: str
    question: str
    options: tuple[_QuestionOption, ...]
    multiple: bool = False


@dataclass(slots=True)
class _QuestionAnswer:
    selected: set[str] = field(default_factory=set)
    custom: str | None = None


class QuestionModal(ModalScreen[tuple[QuestionResponse, ...] | None]):
    CSS = """
    QuestionModal {
        align: center middle;
    }
    #question-dialog {
        width: 92;
        height: 70%;
        min-height: 18;
        max-height: 34;
        padding: 1 2 0 2;
        border: thick $surface;
        background: $background;
    }
    #question-tabs {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    #question-progress {
        height: auto;
        color: $accent;
    }
    #question-title {
        height: auto;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    #question-options {
        height: 1fr;
        border: solid $panel;
        margin-bottom: 1;
    }
    #question-custom {
        display: none;
        margin-bottom: 1;
    }
    #question-review {
        display: none;
        height: 1fr;
        border: solid $panel;
        padding: 1;
        overflow-y: auto;
    }
    #question-help {
        height: auto;
        color: $text-muted;
        padding: 0 1;
    }
    #question-buttons {
        height: auto;
        align: right middle;
        padding: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Cancel", show=False),
        Binding("left", "previous_question", "Previous", show=False),
        Binding("right", "next_question", "Next", show=False),
    ]

    def __init__(self, event: EventEnvelope) -> None:
        super().__init__()
        self.event = event
        raw_questions = event.payload.get("questions")
        self.questions = self._parse_questions(raw_questions)
        self.answers = [_QuestionAnswer() for _ in self.questions]
        self.page_index = 0

    @staticmethod
    def _parse_questions(raw_questions: object) -> tuple[_QuestionPage, ...]:
        if not isinstance(raw_questions, list):
            return ()
        questions: list[_QuestionPage] = []
        for index, raw_question in enumerate(raw_questions):
            question = cast(dict[str, object], raw_question) if isinstance(raw_question, dict) else {}
            raw_options = question.get("options")
            options: list[_QuestionOption] = []
            if isinstance(raw_options, list):
                for raw_option in raw_options:
                    option = cast(dict[str, object], raw_option) if isinstance(raw_option, dict) else {}
                    label = option.get("label")
                    if not isinstance(label, str) or not label.strip():
                        continue
                    description = option.get("description")
                    options.append(
                        _QuestionOption(
                            label=label.strip(),
                            description=description.strip() if isinstance(description, str) else "",
                        )
                    )
            header = question.get("header")
            prompt = question.get("question")
            questions.append(
                _QuestionPage(
                    header=header.strip() if isinstance(header, str) and header.strip() else f"Question {index + 1}",
                    question=prompt.strip() if isinstance(prompt, str) and prompt.strip() else "Choose an answer",
                    options=tuple(options),
                    multiple=question.get("multiple") is True,
                )
            )
        return tuple(questions)

    def compose(self) -> ComposeResult:
        with Vertical(id="question-dialog"):
            yield Static("", id="question-tabs")
            yield Label("", id="question-progress")
            yield Static("", id="question-title")
            yield OptionList(id="question-options")
            yield Input(placeholder="Type a custom answer, then press Enter", id="question-custom")
            yield Static("", id="question-review")
            yield Static("", id="question-help")
            with Horizontal(id="question-buttons"):
                yield Button("Cancel", id="question-cancel")
                yield Button("← Back", id="question-back")
                yield Button("Next →", variant="primary", id="question-next")
                yield Button("Submit", variant="success", id="question-submit")

    def on_mount(self) -> None:
        if not self.questions:
            self.dismiss(None)
            return
        self._render_page()

    @property
    def _on_review(self) -> bool:
        return self.page_index >= len(self.questions)

    def _render_page(self) -> None:
        tabs = self.query_one("#question-tabs", Static)
        tabs.update(self._tabs_text())
        options = self.query_one("#question-options", OptionList)
        custom = self.query_one("#question-custom", Input)
        review = self.query_one("#question-review", Static)
        back = self.query_one("#question-back", Button)
        next_button = self.query_one("#question-next", Button)
        submit = self.query_one("#question-submit", Button)
        back.disabled = self.page_index == 0

        if self._on_review:
            self.query_one("#question-progress", Label).update("Review answers")
            self.query_one("#question-title", Static).update("Confirm before continuing")
            options.display = False
            custom.display = False
            review.display = True
            review.update(self._review_text())
            next_button.display = False
            submit.display = True
            self.query_one("#question-help", Static).update("↑/↓ scroll · Enter submit · ← back · Esc cancel")
            submit.focus()
            return

        page = self.questions[self.page_index]
        answer = self.answers[self.page_index]
        choice = "Multiple choice" if page.multiple else "Single choice"
        self.query_one("#question-progress", Label).update(f"Question {self.page_index + 1} of {len(self.questions)} · {choice}")
        self.query_one("#question-title", Static).update(page.question)
        review.display = False
        options.display = True
        options.clear_options()
        options.add_options(self._option_rows(page, answer))
        custom.value = answer.custom or ""
        custom.display = False
        next_button.display = page.multiple or len(self.questions) > 1
        next_button.label = "Review →" if self.page_index == len(self.questions) - 1 else "Next →"
        submit.display = False
        action = "Space/Enter toggle" if page.multiple else "Enter select"
        self.query_one("#question-help", Static).update(f"↑/↓ move · {action} · ←/→ question · Esc cancel")
        options.highlighted = 0
        options.focus()

    def _tabs_text(self) -> Text:
        text = Text()
        for index, question in enumerate(self.questions):
            answered = self._answer_values(index)
            style = "bold cyan" if index == self.page_index else "green" if answered else "dim"
            marker = "✓" if answered else str(index + 1)
            text.append(f"[{marker} {question.header}]", style=style)
            text.append(" ")
        if len(self.questions) > 1 or any(question.multiple for question in self.questions):
            text.append("[Review]", style="bold cyan" if self._on_review else "dim")
        return text

    @staticmethod
    def _option_rows(page: _QuestionPage, answer: _QuestionAnswer) -> list[Option]:
        rows: list[Option] = []
        for index, option in enumerate(page.options):
            selected = option.label in answer.selected
            marker = ("☑" if selected else "☐") if page.multiple else ("◉" if selected else "○")
            prompt = Text(f"{marker} {option.label}", style="bold" if selected else "")
            if option.description:
                prompt.append(f"\n   {option.description}", style="dim")
            rows.append(Option(prompt, id=f"option-{index}"))
        other_selected = answer.custom is not None
        marker = ("☑" if other_selected else "☐") if page.multiple else ("◉" if other_selected else "○")
        other = Text(f"{marker} Other…", style="bold" if other_selected else "")
        if answer.custom:
            other.append(f"\n   {answer.custom}", style="dim")
        rows.append(Option(other, id="other"))
        return rows

    def _answer_values(self, index: int) -> tuple[str, ...]:
        answer = self.answers[index]
        ordered = [option.label for option in self.questions[index].options if option.label in answer.selected]
        if answer.custom:
            ordered.append(answer.custom)
        return tuple(ordered)

    def _review_text(self) -> Text:
        text = Text()
        for index, page in enumerate(self.questions):
            values = self._answer_values(index)
            text.append(f"{index + 1}. {page.header}\n", style="bold cyan")
            text.append(f"   {page.question}\n")
            if values:
                text.append(f"   {'; '.join(values)}\n", style="green")
            else:
                text.append("   Unanswered\n", style="bold yellow")
            if index < len(self.questions) - 1:
                text.append("\n")
        return text

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_previous_question(self) -> None:
        if self.page_index > 0:
            self.page_index -= 1
            self._render_page()

    def action_next_question(self) -> None:
        self._advance()

    def on_key(self, event: events.Key) -> None:
        if event.key == "space" and not self._on_review and self.questions[self.page_index].multiple:
            options = self.query_one("#question-options", OptionList)
            if options.has_focus and options.highlighted is not None:
                self._select_option(options.highlighted)
                event.stop()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "question-options":
            self._select_option(event.option_index)

    def _select_option(self, option_index: int) -> None:
        if self._on_review:
            return
        page = self.questions[self.page_index]
        answer = self.answers[self.page_index]
        if option_index >= len(page.options):
            custom = self.query_one("#question-custom", Input)
            custom.display = True
            custom.value = answer.custom or ""
            custom.focus()
            return
        label = page.options[option_index].label
        if page.multiple:
            if label in answer.selected:
                answer.selected.remove(label)
            else:
                answer.selected.add(label)
            self._render_page()
            self.query_one("#question-options", OptionList).highlighted = option_index
            return
        answer.selected = {label}
        answer.custom = None
        self._advance()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "question-custom" or self._on_review:
            return
        value = event.value.strip()
        if not value:
            self.notify("Custom answer cannot be empty", severity="warning")
            return
        page = self.questions[self.page_index]
        answer = self.answers[self.page_index]
        answer.custom = value
        if not page.multiple:
            answer.selected.clear()
            self._advance()
        else:
            self._render_page()

    def _advance(self) -> None:
        if self._on_review:
            self._submit()
            return
        if not self._answer_values(self.page_index):
            self.notify("Choose an option or provide a custom answer", severity="warning")
            return
        if len(self.questions) == 1 and not self.questions[0].multiple:
            self._submit()
            return
        self.page_index = min(self.page_index + 1, len(self.questions))
        self._render_page()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "question-cancel":
            self.dismiss(None)
        elif event.button.id == "question-back":
            self.action_previous_question()
        elif event.button.id == "question-next":
            self._advance()
        elif event.button.id == "question-submit":
            self._submit()

    def _submit(self) -> None:
        unanswered = [index for index in range(len(self.questions)) if not self._answer_values(index)]
        if unanswered:
            self.page_index = unanswered[0]
            self._render_page()
            self.notify("Answer every question before submitting", severity="warning")
            return
        self.dismiss(
            tuple(QuestionResponse(header=question.header, answers=self._answer_values(index)) for index, question in enumerate(self.questions))
        )


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
