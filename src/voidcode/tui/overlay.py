"""Pure overlay models: approval, question wizard, and session picker.

Overlays are *models*, not widgets: they render fixed-width rows and consume
decoded keys, returning an :class:`OverlayOutcome`. They never touch the
terminal, ``rich`` renderables, or ``app`` -- the app paints :meth:`Overlay.render`
at the composer position (omp splices overlays over the transcript viewport,
``tui.ts:2496-2528``) or borrows the alternate screen when
:meth:`Overlay.wants_fullscreen` says the rows cannot fit inline (omp's
``fullscreen`` overlay option, ``tui.ts:411-420``).

Behaviour is a straight port of the Textual modals these replace (the old
``ApprovalModal`` / ``QuestionModal`` / ``SessionListModal``), with omp's overlay
grammar for the chrome
(``overlays/overlay-box.ts`` ``OverlayPanel``; ``overlays/ask-dialog.ts``;
``overlays/session-selector.ts``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..runtime.contracts import ReviewCommitLogEntry
from .keys import Key
from .term import clamp_row, escape_terminal_controls, visible_width, wrap_row
from .theme import Theme, resolve_theme
from .transcript import short_session_id

__all__ = [
    "ApprovalOverlay",
    "HelpOverlay",
    "Overlay",
    "OverlayOutcome",
    "OverlayOutcomeKind",
    "PromptHistoryOverlay",
    "QuestionOverlay",
    "AppearanceOverlay",
    "ToolArtifactOverlay",
    "ReviewPresetPickerOverlay",
    "ReviewBranchPickerOverlay",
    "ReviewCommitPickerOverlay",
    "ReviewCustomPromptOverlay",
    "SideQuestionOverlay",
    "CopyPickerOverlay",
    "ExportPickerOverlay",
    "ExportPathOverlay",
    "RenameSessionOverlay",
    "ApprovePickerOverlay",
    "ModelPickerOverlay",
    "PermissionsPickerOverlay",
    "SkillsMenuOverlay",
    "SkillsManageOverlay",
    "SkillsToggleItem",
]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class OverlayOutcomeKind(StrEnum):
    """Lifecycle state of an overlay after a key."""

    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OverlayOutcome:
    """Result of :meth:`Overlay.handle_key`.

    ``payload`` is overlay-specific and only meaningful when ``kind`` is
    :attr:`OverlayOutcomeKind.DONE` (see each overlay's docstring).
    """

    kind: OverlayOutcomeKind = OverlayOutcomeKind.PENDING
    payload: object = None


_PENDING = OverlayOutcome()
_CANCELLED = OverlayOutcome(OverlayOutcomeKind.CANCELLED)


def _done(payload: object) -> OverlayOutcome:
    return OverlayOutcome(OverlayOutcomeKind.DONE, payload)


class Overlay(Protocol):
    """Anything the app can splice inline or borrow the alt screen for."""

    def render(self, width: int, /) -> Sequence[str]:
        """The overlay's own rows, each at most ``width`` cells wide."""
        ...

    def viewport(self, width: int, height: int, /) -> list[str]:
        """The rows visible at the current terminal geometry."""
        ...

    def handle_key(self, key: Key, /) -> OverlayOutcome:
        """Consume one decoded key."""
        ...

    def cursor_position(self, width: int, /) -> tuple[int, int] | None:
        """Optional native editable cursor in overlay-local coordinates."""
        ...

    def wants_fullscreen(self, width: int, height: int, /) -> bool:
        """True when inline rows would not fit the available ``height``."""
        ...


# ---------------------------------------------------------------------------
# Shared rendering helpers
# ---------------------------------------------------------------------------

#: One-column inset on each side of a framed panel's content
#: (``overlay-box.ts`` ``row()``).
_PANEL_INSET = 1


def _body_width(width: int) -> int:
    """Content width inside a framed panel of total ``width``."""
    return max(0, width - 2 * (_PANEL_INSET + 1))


def _default_theme() -> Theme:
    return resolve_theme()


class _Base:
    """State + panel chrome shared by the concrete overlays."""

    __slots__ = ("_theme",)

    def __init__(self, theme: Theme | None) -> None:
        self._theme = theme if theme is not None else _default_theme()

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        del width
        return None

    def render(self, width: int) -> Sequence[str]:
        raise NotImplementedError

    def viewport(self, width: int, height: int) -> list[str]:
        """Keep panel heading and controls visible within the terminal height."""
        rows = list(self.render(width))
        if len(rows) <= height or height < 4:
            return rows
        body = rows[1:-1]
        slots = height - 2
        if slots < 2:
            return [rows[0], *body[:slots], rows[-1]][:height]
        middle = body[1:-1]
        return [rows[0], body[0], *middle[: slots - 2], body[-1], rows[-1]]

    # -- styling -----------------------------------------------------------

    def _fg(self, token: str, text: str) -> str:
        return self._theme.fg(token, text)

    def _bold(self, token: str, text: str) -> str:
        return "\x1b[1m" + self._theme.fg(token, text) + "\x1b[22m"

    def _wrap(self, text: str, width: int) -> list[str]:
        if width <= 0:
            return []
        if not text:
            return [""]
        return wrap_row(text, width, color_system=self._theme.color_system)

    # -- panel chrome (``overlay-box.ts``) ---------------------------------

    def _panel(self, title: str, body: Sequence[str], width: int) -> list[str]:
        if width <= 0:
            return []
        inner = max(0, width - 2)
        avail = max(0, inner - 2 * _PANEL_INSET)
        border = self._theme
        rows = [_top_border(title, width, border)]
        pad = " " * _PANEL_INSET
        for line in body:
            content = clamp_row(line, avail)
            filler = " " * max(0, avail - visible_width(content))
            rows.append(border.fg("border", "│") + pad + content + filler + pad + border.fg("border", "│"))
        rows.append(_bottom_border(width, border))
        return [clamp_row(row, width) for row in rows]


class HelpOverlay(_Base):
    """Read-only help for actual key bindings and runtime-owned actions."""

    __slots__ = ("_bindings", "_scroll", "_settings")

    def __init__(self, theme: Theme | None, bindings: Mapping[str, str], settings: Sequence[str] = ()) -> None:
        super().__init__(theme)
        self._bindings = bindings
        self._settings = tuple(settings)
        self._scroll = 0

    def render(self, width: int) -> Sequence[str]:
        body = [self._fg("text", f"{key.upper()}  {action}") for action, key in sorted(self._bindings.items(), key=lambda item: item[1])]
        body.extend(self._fg("dim", setting) for setting in self._settings)
        body.extend(
            (
                self._fg("text", "Esc  Cancel the active turn or close this view"),
                self._fg("text", "Enter  Submit; while running, queue steering"),
                self._fg("text", "Approval target, reason, and actions remain visible"),
                _hint("Press Esc to close", self._theme),
            )
        )
        return self._panel("VoidCode Help", body, width)

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name in {"up", "down", "pageUp", "pageDown"}:
            delta = -1 if key.name in {"up", "pageUp"} else 1
            self._scroll = max(0, self._scroll + delta * (10 if key.name.startswith("page") else 1))
        return _PENDING

    def viewport(self, width: int, height: int) -> list[str]:
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        primary = list(self._settings[:2])
        browsable = [
            *(f"{key.upper()}  {action}" for action, key in sorted(self._bindings.items(), key=lambda item: item[1])),
            *self._settings[2:],
            "Esc  Cancel the active turn or close this view",
            "Enter  Submit; while running, queue steering",
            "Approval target, reason, and actions remain visible",
        ]
        footer = _hint("↑↓ · Esc close" if _body_width(width) <= 16 else "↑/↓ scroll · Esc close", self._theme)
        scroll_slots = max(0, capacity - len(primary) - 1)
        self._scroll = min(self._scroll, max(0, len(browsable) - scroll_slots))
        body = [
            *(_hint(setting, self._theme) for setting in primary),
            *(self._fg("text", line) for line in browsable[self._scroll : self._scroll + scroll_slots]),
            footer,
        ]
        return self._panel("VoidCode Help", body[:capacity], width)

    def wants_fullscreen(self, width: int, height: int) -> bool:
        return len(self.render(width)) > height


def _top_border(title: str, width: int, theme: Theme) -> str:
    inner = max(0, width - 2)
    left = theme.symbol("boxRound.topLeft")
    right = theme.symbol("boxRound.topRight")
    rule = theme.symbol("boxRound.horizontal")
    label = "" if not title else f" {title.strip()} "
    label_width = visible_width(label)
    if label_width and label_width + 1 <= inner:
        between = rule + label + rule * max(0, inner - 1 - label_width)
    else:
        between = rule * inner
    return theme.fg("border", left + between + right)


def _bottom_border(width: int, theme: Theme) -> str:
    inner = max(0, width - 2)
    left = theme.symbol("boxRound.bottomLeft")
    right = theme.symbol("boxRound.bottomRight")
    rule = theme.symbol("boxRound.horizontal")
    return theme.fg("border", left + rule * inner + right)


def _hint(text: str, theme: Theme) -> str:
    return theme.fg("dim", text)


def _typed_text(key: Key) -> str:
    """Printable text a key inserts, or ``""``.

    The decoder sets ``Key.text`` for printable input; named printable keys
    (``"space"``, ``"a"``) also carry it. A caller-constructed ``Key("a")``
    without ``text`` still types, since printable names are their own text.
    """
    if key.text and key.text.isprintable():
        return key.text
    if len(key.name) == 1 and key.name.isprintable():
        return key.name
    return ""


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class ApprovalOverlay(_Base):
    """Tool-approval dialog (port of the old ``ApprovalModal``).

    ``y`` allows, ``n`` and ``escape`` deny. Every cancel/dismiss path maps to
    :attr:`OverlayOutcomeKind.DONE` with payload ``"deny"`` -- the old modal's
    ``escape`` binding literally returned ``"deny"``, and a programmatic
    dismissal (``None``) was treated as deny by the app
    (``app.py:1226-1228``). The app worker must keep mapping
    :attr:`OverlayOutcomeKind.CANCELLED` to ``"deny"`` as well.

    DONE payload: ``"allow" | "deny"`` (a string).
    """

    __slots__ = ("_detail_scroll", "_arguments", "_reason", "_target", "_tool")

    def __init__(
        self,
        *,
        tool: str,
        target: str,
        reason: str,
        arguments: str,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._tool = tool
        self._target = target
        self._reason = reason
        self._arguments = arguments
        self._detail_scroll = 0

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        tool = self._tool or "tool call"
        header = f"Approve {tool} for {self._target}?" if self._target else f"Approve {tool}?"
        body = self._wrap(self._bold("text", header), avail)
        if self._reason:
            body.extend(self._wrap(self._fg("warning", f"Why: {self._reason}"), avail))
        if self._arguments.strip():
            body.append("")
            rail = theme.fg("border", theme.symbol("boxRound.vertical"))
            for line in self._arguments.splitlines() or [""]:
                body.extend(self._wrap(f"  {rail} {theme.fg('dim', line)}", avail))
        body.append("")
        allow = theme.symbol("status.success")
        deny = theme.symbol("status.error")
        body.append(
            _hint(
                f"{allow} y Allow   {deny} n Deny   {theme.symbol('icon.esc')} esc Deny",
                theme,
            )
        )
        return self._panel(f"Approve {tool}", body, width)

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name in {"up", "down", "pageUp", "pageDown"}:
            delta = -1 if key.name in {"up", "pageUp"} else 1
            self._detail_scroll = max(0, self._detail_scroll + delta * (10 if key.name.startswith("page") else 1))
            return _PENDING
        if key.name == "y":
            return _done("allow")
        if key.name in ("n", "escape"):
            return _done("deny")
        return _PENDING

    def wants_fullscreen(self, width: int, height: int) -> bool:
        return len(self.render(width)) > height

    def viewport(self, width: int, height: int) -> list[str]:
        avail = _body_width(width)
        capacity = max(0, height - 2)
        if avail <= 0 or capacity == 0:
            return []
        theme = self._theme
        details: list[str] = []
        if self._target:
            details.extend(self._wrap(f"Target: {escape_terminal_controls(self._target)}", avail))
        if self._reason:
            details.extend(self._wrap(f"Why: {escape_terminal_controls(self._reason)}", avail))
        if self._arguments.strip():
            rail = theme.fg("border", theme.symbol("boxRound.vertical"))
            for line in self._arguments.splitlines() or [""]:
                details.extend(self._wrap(f"  {rail} {theme.fg('dim', escape_terminal_controls(line))}", avail))
        footer = _hint("y Allow · n Deny", theme)
        count = max(0, capacity - 1)
        self._detail_scroll = min(self._detail_scroll, max(0, len(details) - count))
        body = [*details[self._detail_scroll : self._detail_scroll + count], footer]
        title = f"Approve {self._tool or 'tool call'}"
        if visible_width(title) > width - 3:
            title = "Approval ↑↓"
        return self._panel(title, body, width)


class GoalConfirmOverlay(_Base):
    __slots__ = ("_objective",)

    def __init__(self, *, objective: str, theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._objective = objective

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        body = self._wrap(self._bold("text", "Replace goal?"), avail)
        truncated_obj = self._objective[:200] + ("..." if len(self._objective) > 200 else "")
        body.extend(self._wrap(f"New objective: {truncated_obj}", avail))
        body.append("")
        body.append("Replace current goal: Set the new objective and start it now")
        body.append("Cancel: Keep the current goal")
        body.append("")
        allow = theme.symbol("status.success")
        deny = theme.symbol("status.error")
        body.append(_hint(f"{allow} y Replace   {deny} n Cancel   {theme.symbol('icon.esc')} esc Cancel", theme))
        return self._panel("Replace goal?", body, width)

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "y":
            return _done("replace")
        if key.name in ("n", "escape"):
            return _done("cancel")
        return _PENDING

    def wants_fullscreen(self, width: int, height: int) -> bool:
        _ = (width, height)
        return False


# ---------------------------------------------------------------------------
# Question wizard
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Option:
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class _Page:
    header: str
    question: str
    options: tuple[_Option, ...]
    multiple: bool = False


@dataclass(slots=True)
class _Answer:
    selected: set[str] = field(default_factory=set)


def _parse_questions(raw: object) -> tuple[_Page, ...]:
    """Port of the old ``QuestionModal._parse_questions``."""
    if not isinstance(raw, list):
        return ()
    pages: list[_Page] = []
    for index, raw_question in enumerate(raw):
        question = raw_question if isinstance(raw_question, dict) else {}
        options: list[_Option] = []
        raw_options = question.get("options")
        if isinstance(raw_options, list):
            for raw_option in raw_options:
                option = raw_option if isinstance(raw_option, dict) else {}
                label = option.get("label")
                if not isinstance(label, str) or not label.strip():
                    continue
                description = option.get("description")
                options.append(
                    _Option(
                        label=label.strip(),
                        description=description.strip() if isinstance(description, str) else "",
                    )
                )
        header = question.get("header")
        prompt = question.get("question")
        pages.append(
            _Page(
                header=header.strip() if isinstance(header, str) and header.strip() else f"Question {index + 1}",
                question=prompt.strip() if isinstance(prompt, str) and prompt.strip() else "Choose an answer",
                options=tuple(options),
                multiple=question.get("multiple") is True,
            )
        )
    return tuple(pages)


class QuestionOverlay(_Base):
    """Multi-page question wizard (port of the old ``QuestionModal``).

    Per-question header, single- and multi-select rows, a page/progress
    indicator, a final Review page, and submit blocked while any question is
    unanswered. Every answer is one of the declared option labels -- the runtime
    (``QuestionTool.validate_responses``) accepts nothing else, so the wizard
    offers nothing else. ``escape`` cancels.

    DONE payload: ``tuple[tuple[str, tuple[str, ...]], ...]`` -- ``(header,
    answers)`` per question, in question order.
    """

    __slots__ = ("_answers", "_highlight", "_page_index", "_questions", "_review_scroll")

    def __init__(
        self,
        *,
        questions: Sequence[object],
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._questions = _parse_questions(list(questions))
        self._answers = [_Answer() for _ in self._questions]
        self._page_index = 0
        self._reset_view()

    # -- state -------------------------------------------------------------

    @property
    def _on_review(self) -> bool:
        return self._page_index >= len(self._questions)

    def _answer_values(self, index: int) -> tuple[str, ...]:
        answer = self._answers[index]
        return tuple(option.label for option in self._questions[index].options if option.label in answer.selected)

    def _reset_view(self) -> None:
        self._highlight = 0
        self._review_scroll = 0

    # -- key handling ------------------------------------------------------

    def handle_key(self, key: Key) -> OverlayOutcome:
        if not self._questions:
            return _CANCELLED
        if key.name == "escape":
            return _CANCELLED
        if self._on_review:
            return self._handle_review_key(key)
        if key.name in ("up", "down"):
            self._move_highlight(-1 if key.name == "up" else 1)
            return _PENDING
        if key.name == "left":
            if self._page_index > 0:
                self._page_index -= 1
                self._reset_view()
            return _PENDING
        if key.name == "right":
            return self._advance()
        if key.name in ("enter", "space"):
            page = self._questions[self._page_index]
            if key.name == "space" and not page.multiple:
                return _PENDING
            return self._select(self._highlight)
        return _PENDING

    def _handle_review_key(self, key: Key) -> OverlayOutcome:
        if key.name == "enter":
            return self._submit()
        if key.name in {"up", "down", "pageUp", "pageDown"}:
            delta = -1 if key.name in {"up", "pageUp"} else 1
            self._review_scroll = max(0, self._review_scroll + delta * (10 if key.name.startswith("page") else 1))
        elif key.name == "left":
            self._page_index = max(0, self._page_index - 1)
            self._reset_view()
        return _PENDING

    def _move_highlight(self, delta: int) -> None:
        page = self._questions[self._page_index]
        count = len(page.options)
        self._highlight = max(0, min(count - 1, self._highlight + delta))

    def _select(self, index: int) -> OverlayOutcome:
        page = self._questions[self._page_index]
        answer = self._answers[self._page_index]
        if index >= len(page.options):
            return _PENDING
        label = page.options[index].label
        if page.multiple:
            if label in answer.selected:
                answer.selected.discard(label)
            else:
                answer.selected.add(label)
            self._highlight = index
            return _PENDING
        answer.selected = {label}
        return self._advance()

    def _advance(self) -> OverlayOutcome:
        if self._on_review:
            return self._submit()
        if not self._answer_values(self._page_index):
            return _PENDING
        if len(self._questions) == 1 and not self._questions[0].multiple:
            return self._submit()
        self._page_index = min(self._page_index + 1, len(self._questions))
        self._reset_view()
        return _PENDING

    def _submit(self) -> OverlayOutcome:
        unanswered = [index for index in range(len(self._questions)) if not self._answer_values(index)]
        if unanswered:
            self._page_index = unanswered[0]
            self._reset_view()
            return _PENDING
        payload = tuple((page.header, self._answer_values(index)) for index, page in enumerate(self._questions))
        return _done(payload)

    # -- rendering ---------------------------------------------------------

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        if not self._questions:
            return self._panel("Ask", [_hint("No questions to answer.", theme)], width)
        body: list[str] = list(self._wrap(self._tabs_row(), avail))
        if self._on_review:
            body.append(self._fg("accent", "Review answers"))
            body.extend(self._wrap(self._bold("text", "Confirm before continuing"), avail))
            body.append("")
            body.extend(self._review_lines(avail))
            body.append("")
            body.append(_hint("Enter submit · ↑/↓ scroll · ← back · Esc cancel", theme))
            return self._panel("Ask", body, width)

        page = self._questions[self._page_index]
        kind = "Multiple choice" if page.multiple else "Single choice"
        body.append(self._fg("accent", f"Question {self._page_index + 1} of {len(self._questions)} · {kind}"))
        body.extend(self._wrap(self._bold("text", page.question), avail))
        body.append("")
        for index, option in enumerate(page.options):
            body.extend(self._option_lines(index, option.label, option.description, avail))
        body.append("")
        body.append(_hint(self._help_text(page), theme))
        return self._panel("Ask", body, width)

    def viewport(self, width: int, height: int) -> list[str]:
        avail = _body_width(width)
        capacity = max(0, height - 2)
        if avail <= 0 or capacity == 0:
            return []
        if not self._questions:
            return self._panel("Ask", [_hint("No questions to answer.", self._theme)][:capacity], width)
        theme = self._theme
        if self._on_review:
            lines = self._review_lines(avail)
            if avail <= 16:
                footer = [_hint("↑↓ scroll", theme), _hint("Enter submit", theme), _hint("Esc cancel", theme)]
                available = max(0, capacity - len(footer))
                self._review_scroll = min(self._review_scroll, max(0, len(lines) - available))
                body = [*lines[self._review_scroll : self._review_scroll + available], *footer]
            else:
                heading = _hint("Review answers", theme)
                footer = _hint("Enter submit · ↑/↓ scroll · ← back · Esc cancel", theme)
                available = max(0, capacity - 2)
                self._review_scroll = min(self._review_scroll, max(0, len(lines) - available))
                body = [heading, *lines[self._review_scroll : self._review_scroll + available], footer]
            return self._panel("Ask", body[:capacity], width)
        page = self._questions[self._page_index]
        question = clamp_row(self._bold("text", escape_terminal_controls(page.question)), avail)
        option_rows: list[str] = []
        for index, option in enumerate(page.options):
            selected = option.label in self._answers[self._page_index].selected
            cursor = theme.symbol("nav.cursor") if index == self._highlight else " "
            if page.multiple:
                marker = theme.symbol("checkbox.checked" if selected else "checkbox.unchecked")
            else:
                marker = theme.symbol("radio.selected" if selected else "radio.unselected")
            label = option.label
            if option.description:
                label += f" — {option.description}"
            row = f"{cursor} {marker} {escape_terminal_controls(label)}"
            option_rows.append(theme.fg("accent" if index == self._highlight else "text", clamp_row(row, avail)))
        if avail <= 16:
            footer = (
                (_hint("↑↓ Space toggle", theme), _hint("→next Esc cancel", theme))
                if page.multiple
                else (_hint("↑↓ Enter select", theme), _hint("Esc cancel", theme))
            )
            option_slots = max(0, capacity - 1 - len(footer))
            start = max(0, min(self._highlight, len(option_rows) - option_slots))
            body = [question, *option_rows[start : start + option_slots], *footer]
        else:
            heading = _hint(f"Q{self._page_index + 1}/{len(self._questions)} {page.header} · {'multiple' if page.multiple else 'single'}", theme)
            footer = _hint(self._help_text(page), theme)
            option_slots = max(0, capacity - 3)
            start = max(0, min(self._highlight, len(option_rows) - option_slots))
            body = [heading, question, *option_rows[start : start + option_slots], footer]
        return self._panel("Ask", body[:capacity], width)

    def _tabs_row(self) -> str:
        theme = self._theme
        parts: list[str] = []
        for index, page in enumerate(self._questions):
            answered = bool(self._answer_values(index))
            if index == self._page_index:
                token = "accent"
            elif answered:
                token = "success"
            else:
                token = "dim"
            marker = theme.symbol("status.success") if answered else str(index + 1)
            parts.append(theme.fg(token, f"[{marker} {page.header}]"))
        if len(self._questions) > 1 or any(page.multiple for page in self._questions):
            token = "accent" if self._on_review else "dim"
            parts.append(theme.fg(token, "[Review]"))
        return " ".join(parts)

    def _option_lines(self, index: int, label: str, description: str | None, avail: int) -> list[str]:
        theme = self._theme
        page = self._questions[self._page_index]
        answer = self._answers[self._page_index]
        selected = label in answer.selected
        if page.multiple:
            glyph = theme.symbol("checkbox.checked" if selected else "checkbox.unchecked")
        else:
            glyph = theme.symbol("radio.selected" if selected else "radio.unselected")
        cursor = theme.symbol("nav.cursor") if index == self._highlight else " "
        head = f"{cursor} {glyph} {label}"
        if selected:
            head = self._bold("accent", head)
        else:
            head = theme.fg("text", head)
        lines = self._wrap(head, avail)
        if description:
            lines.extend(self._wrap(theme.fg("dim", f"      {description}"), avail))
        return lines

    def _help_text(self, page: _Page) -> str:
        action = "Space/Enter toggle" if page.multiple else "Enter select"
        return f"↑/↓ move · {action} · ←/→ question · Esc cancel"

    def _review_lines(self, avail: int) -> list[str]:
        theme = self._theme
        lines: list[str] = []
        for index, page in enumerate(self._questions):
            lines.extend(self._wrap(self._bold("accent", f"{index + 1}. {page.header}"), avail))
            lines.extend(self._wrap(theme.fg("text", f"   {page.question}"), avail))
            values = self._answer_values(index)
            if values:
                lines.extend(self._wrap(theme.fg("success", f"   {'; '.join(values)}"), avail))
            else:
                lines.extend(self._wrap(theme.fg("warning", "   Unanswered"), avail))
            if index < len(self._questions) - 1:
                lines.append("")
        return lines

    def wants_fullscreen(self, width: int, height: int) -> bool:
        return len(self.render(width)) > height


# ---------------------------------------------------------------------------
# Session picker
# ---------------------------------------------------------------------------


class SessionPickerOverlay(_Base):
    """Session list picker (port of the old ``SessionListModal``).

    The prompt title is the label; typing filters (omp's session selector
    carries incremental search, ``overlays/session-selector.ts:300-345`` and
    ``:426-476``: a query is tokenized on whitespace and every token must
    appear in the row's haystack). ``escape`` cancels; ``enter`` on a row
    resumes it.

    DONE payload: the session id (``str``).

    ``wants_fullscreen`` is always ``True``: the session selector uses its own
    fullscreen viewport rather than clipping the selectable session list.

    ``depths`` maps a session id to its fork-forest depth; a row indents two
    spaces per level (the CLI's ``sessions tree`` convention) and a missing id
    renders at depth 0. Indentation is visual only -- the filter haystack is
    the bare id/title.
    """

    __slots__ = ("_action", "_depths", "_filter", "_highlight", "_sessions", "_title")

    def __init__(
        self,
        *,
        sessions: Sequence[tuple[str, str]],
        depths: Mapping[str, int] | None = None,
        theme: Theme | None = None,
        title: str = "Select Session",
        action: str = "resume",
    ) -> None:
        super().__init__(theme)
        self._sessions = tuple(sessions)
        self._depths = dict(depths) if depths is not None else {}
        self._filter = ""
        self._highlight = 0
        self._title = title
        self._action = action

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._filter))))

    def _matches(self) -> list[tuple[str, str]]:
        tokens = self._filter.lower().split()
        if not tokens:
            return list(self._sessions)
        matched: list[tuple[str, str]] = []
        for session_id, title in self._sessions:
            haystack = f"{session_id} {title}".lower()
            if all(token in haystack for token in tokens):
                matched.append((session_id, title))
        return matched

    def handle_key(self, key: Key) -> OverlayOutcome:
        matches = self._matches()
        if key.name == "escape":
            return _CANCELLED
        if key.name == "up":
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name == "down":
            self._highlight = min(max(0, len(matches) - 1), self._highlight + 1)
            return _PENDING
        if key.name == "backspace":
            self._filter = self._filter[:-1]
            self._highlight = 0
            return _PENDING
        if key.name == "enter":
            if matches:
                return _done(matches[min(self._highlight, len(matches) - 1)][0])
            return _CANCELLED
        text = _typed_text(key)
        if text:
            self._filter += text
            self._highlight = 0
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        cursor = theme.symbol("nav.cursor")
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._filter))]
        body.append("")
        if not self._sessions:
            body.append(theme.fg("warning", "No sessions found."))
        elif not matches:
            body.append(theme.fg("dim", "No matching sessions."))
        else:
            for index, (session_id, title) in enumerate(matches):
                pointer = cursor if index == self._highlight else " "
                # Two spaces per fork level, matching the CLI's ``sessions tree`` indent.
                label = f"{pointer} {'  ' * self._depths.get(session_id, 0)}{title}"
                styled = self._bold("accent", label) if index == self._highlight else theme.fg("text", label)
                short = short_session_id(session_id)
                styled += theme.fg("dim", f"  ({short})")
                body.extend(self._wrap(styled, avail))
        body.append("")
        action = self._action
        body.append(_hint(f"↑/↓ move · Enter {action} · type to filter · Esc cancel", theme))
        return self._panel(self._title, body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return True

    def viewport(self, width: int, height: int) -> list[str]:
        if _body_width(width) > 16 and len(self.render(width)) <= height:
            return super().viewport(width, height)
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        body = [self._theme.fg("accent", "> " + escape_terminal_controls(self._filter))]
        if matches:
            session_id, title = matches[self._highlight]
            pointer = self._theme.symbol("nav.cursor")
            body.append(self._bold("accent", f"{pointer} {title}  ({short_session_id(session_id)})"))
        elif self._sessions:
            body.append(self._theme.fg("dim", "No matching sessions."))
        else:
            body.append(self._theme.fg("warning", "No sessions found."))
        body.extend((_hint(f"↑↓ Enter {self._action}", self._theme), _hint("Esc cancel·type", self._theme)))
        return self._panel(self._title, body[:capacity], width)


#: Short, selector-width labels for the raw runtime event types (the full names
#: overrun the row). Anything unlisted renders its own name.
_ENTRY_LABELS: Mapping[str, str] = {
    "runtime.request_received": "user",
    "core.response_ready": "assistant",
    "core.provider_stream": "stream",
    "core.tool_request_created": "tool",
    "runtime.tool_started": "tool",
    "runtime.tool_progress": "tool",
    "runtime.tool_completed": "tool",
}


def _entry_label(event_type: str) -> str:
    return _ENTRY_LABELS.get(event_type, event_type)


class SessionEntryLike(Protocol):
    """Structural view of the runtime's ``SessionEntrySummary``.

    The runtime owns the real dataclass; the overlay only reads these four
    attributes, so it takes any object that has them (a test stub included).
    Read-only properties, not attributes: the runtime's type is frozen, so a
    writable protocol member would not accept it.
    """

    @property
    def sequence(self) -> int: ...
    @property
    def event_type(self) -> str: ...
    @property
    def on_current_path(self) -> bool: ...
    @property
    def preview(self) -> str: ...


class TreeSelectorOverlay(_Base):
    """Session-entry (checkout) selector -- ``app.session.tree``.

    Rows are the session's entries in the order the runtime projects them:
    ascending sequence, never re-sorted, so a row can never appear above its
    parent (the listing arrives root-first and a parent always has the lower
    sequence). ``*`` marks the current leaf; an entry outside the root->leaf
    chain is dimmed and suffixed ``abandoned`` -- the two things a checkout has
    to distinguish. Indentation is *not* depth: a flat two-space indent is
    layout only, because an entry's parent is not always the row above it.

    Typing filters (token-AND substring, :meth:`SessionPickerOverlay._matches`),
    ``enter`` picks, ``escape``/backspace as in the picker. The leaf row carries
    the nav cursor; an off-path row is dimmed and suffixed ``abandoned``.

    DONE payload: the chosen ``sequence`` (``int``). ``wants_fullscreen`` is
    always ``True``: like the session picker, this list outgrows the inline
    viewport.
    """

    __slots__ = ("_entries", "_filter", "_highlight", "_leaf")

    def __init__(self, *, entries: Sequence[SessionEntryLike], theme: Theme | None = None) -> None:
        super().__init__(theme)
        # Duck-typed: the runtime's ``SessionEntrySummary`` is not imported here
        # (the runtime owns it); only ``sequence``/``event_type``/``preview``/
        # ``on_current_path`` are read through these accessors. The leaf is the
        # last on-path entry, so no extra runtime call is needed for the marker.
        self._entries = tuple(entries)
        self._leaf = max((entry.sequence for entry in self._entries if entry.on_current_path), default=None)
        self._filter = ""
        self._highlight = 0

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._filter))))

    def _matches(self) -> list[SessionEntryLike]:
        tokens = self._filter.lower().split()
        if not tokens:
            return list(self._entries)
        matched: list[SessionEntryLike] = []
        for entry in self._entries:
            haystack = f"{entry.sequence} {entry.event_type} {entry.preview}".lower()
            if all(token in haystack for token in tokens):
                matched.append(entry)
        return matched

    def handle_key(self, key: Key) -> OverlayOutcome:
        matches = self._matches()
        if key.name == "escape":
            return _CANCELLED
        if key.name == "up":
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name == "down":
            self._highlight = min(max(0, len(matches) - 1), self._highlight + 1)
            return _PENDING
        if key.name == "backspace":
            self._filter = self._filter[:-1]
            self._highlight = 0
            return _PENDING
        if key.name == "enter":
            if matches:
                return _done(matches[min(self._highlight, len(matches) - 1)].sequence)
            return _CANCELLED
        text = _typed_text(key)
        if text:
            self._filter += text
            self._highlight = 0
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        cursor = theme.symbol("nav.cursor")
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._filter))]
        body.append("")
        if not self._entries:
            body.append(theme.fg("warning", "No entries in this session."))
        elif not matches:
            body.append(theme.fg("dim", "No matching entries."))
        else:
            for index, entry in enumerate(matches):
                pointer = cursor if entry.sequence == self._leaf else " "
                on_path = bool(entry.on_current_path)
                text = f"{entry.sequence} {_entry_label(entry.event_type)} {entry.preview}".rstrip()
                label = f"{pointer}  {text}" if on_path else f"{pointer}  {text}  * abandoned"
                styled = self._bold("accent", label) if index == self._highlight else theme.fg("text" if on_path else "dim", label)
                body.extend(self._wrap(styled, avail))
        body.append("")
        body.append(_hint("↑/↓ move · Enter checkout · type to filter · Esc cancel", theme))
        return self._panel("Session Entries", body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return True

    def viewport(self, width: int, height: int) -> list[str]:
        if _body_width(width) > 16 and len(self.render(width)) <= height:
            return super().viewport(width, height)
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        body = [self._theme.fg("accent", "> " + escape_terminal_controls(self._filter))]
        if matches:
            entry = matches[self._highlight]
            pointer = self._theme.symbol("nav.cursor") if entry.sequence == self._leaf else " "
            text = f"{entry.sequence} {_entry_label(entry.event_type)} {entry.preview}".rstrip()
            label = f"{pointer}  {text}" if entry.on_current_path else f"{pointer}  {text}  * abandoned"
            body.append(self._theme.fg("text" if entry.on_current_path else "dim", clamp_row(label, _body_width(width))))
        elif self._entries:
            body.append(self._theme.fg("dim", "No matching entries."))
        else:
            body.append(self._theme.fg("warning", "No entries in this session."))
        body.extend((_hint("↑↓ Enter select", self._theme), _hint("Esc cancel·type", self._theme)))
        return self._panel("Session Entries", body[:capacity], width)


class PromptHistoryOverlay(_Base):
    """Prompt-history search (``app.history.search``).

    The composer already keeps the bounded prompt history; this is the same
    picker as :class:`SessionPickerOverlay` over those entries, with the chosen
    prompt handed back on ``enter``. It filters instead of selecting a session,
    so ``enter`` on an empty filter is a cancel (there is nothing to pick).
    """

    __slots__ = ("_filter", "_highlight", "_entries")

    def __init__(self, *, entries: Sequence[str], theme: Theme | None = None) -> None:
        super().__init__(theme)
        # Newest first: the most recent prompt is what the user usually wants.
        self._entries = tuple(reversed(entries))
        self._filter = ""
        self._highlight = 0

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._filter))))

    def _matches(self) -> list[str]:
        tokens = self._filter.lower().split()
        if not tokens:
            return list(self._entries)
        return [entry for entry in self._entries if all(token in entry.lower() for token in tokens)]

    def handle_key(self, key: Key) -> OverlayOutcome:
        matches = self._matches()
        if key.name == "escape":
            return _CANCELLED
        if key.name == "up":
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name == "down":
            self._highlight = min(max(0, len(matches) - 1), self._highlight + 1)
            return _PENDING
        if key.name == "backspace":
            self._filter = self._filter[:-1]
            self._highlight = 0
            return _PENDING
        if key.name == "enter":
            if matches:
                return _done(matches[min(self._highlight, len(matches) - 1)])
            return _CANCELLED
        text = _typed_text(key)
        if text:
            self._filter += text
            self._highlight = 0
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        cursor = theme.symbol("nav.cursor")
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._filter))]
        body.append("")
        if not self._entries:
            body.append(theme.fg("warning", "No prompt history."))
        elif not matches:
            body.append(theme.fg("dim", "No matching prompts."))
        else:
            for index, entry in enumerate(matches):
                pointer = cursor if index == self._highlight else " "
                label = f"{pointer} {entry}"
                styled = self._bold("accent", label) if index == self._highlight else theme.fg("text", label)
                body.extend(self._wrap(styled, avail))
        body.append("")
        body.append(_hint("↑/↓ move · Enter insert · type to filter · Esc cancel", theme))
        return self._panel("Search Prompt History", body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return len(self.render(_width)) > _height

    def viewport(self, width: int, height: int) -> list[str]:
        if _body_width(width) > 16 and len(self.render(width)) <= height:
            return super().viewport(width, height)
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        body = [self._theme.fg("accent", "> " + escape_terminal_controls(self._filter))]
        if matches:
            body.append(self._bold("accent", clamp_row(f"{self._theme.symbol('nav.cursor')} {matches[self._highlight]}", _body_width(width))))
        elif self._entries:
            body.append(self._theme.fg("dim", "No matching prompts."))
        else:
            body.append(self._theme.fg("warning", "No prompt history."))
        body.extend((_hint("↑↓ Enter insert", self._theme), _hint("Esc cancel·type", self._theme)))
        return self._panel("Search Prompt History", body[:capacity], width)


class AppearanceOverlay(_Base):
    """Temporary appearance preview with an explicit, scoped save action."""

    __slots__ = ("_fields", "_notice", "_selected", "_save_scope", "_scope", "values")

    def __init__(
        self,
        *,
        theme: Theme,
        choices: Mapping[str, Sequence[str]],
        values: Mapping[str, str],
    ) -> None:
        super().__init__(theme)
        self._fields = {name: tuple(options) for name, options in choices.items()}
        self._selected = 0
        self._save_scope = False
        self._scope = "user"
        self.values = dict(values)
        self._notice = ""

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if self._save_scope:
            if key.name in {"left", "right"}:
                self._scope = "workspace" if self._scope == "user" else "user"
                return _PENDING
            if key.name == "enter":
                return _done(("save", self._scope, dict(self.values)))
            if key.name == "s":
                self._save_scope = False
                return _PENDING
            return _PENDING
        if key.name == "up":
            self._selected = max(0, self._selected - 1)
        elif key.name == "down":
            self._selected = min(len(self._fields) - 1, self._selected + 1)
        elif key.name in {"left", "right"}:
            name = tuple(self._fields)[self._selected]
            options = self._fields[name]
            if options:
                index = options.index(self.values[name])
                delta = -1 if key.name == "left" else 1
                self.values[name] = options[(index + delta) % len(options)]
        elif key.name == "s":
            self._save_scope = True
        elif key.name == "enter":
            return _done(("apply", dict(self.values)))
        return _PENDING

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme

    def set_notice(self, notice: str) -> None:
        self._notice = notice

    def render(self, width: int) -> Sequence[str]:
        body: list[str] = []
        if self._save_scope:
            body.append(self._theme.fg("text", "Save appearance to:"))
            for scope in ("user", "workspace"):
                marker = self._theme.symbol("nav.cursor") if scope == self._scope else " "
                body.append(self._theme.fg("accent" if scope == self._scope else "text", f"{marker} {scope}"))
            body.append(_hint("←/→ choose · Enter save · Esc discard preview", self._theme))
            return self._panel("Save Appearance", body, width)
        for index, name in enumerate(self._fields):
            marker = self._theme.symbol("nav.cursor") if index == self._selected else " "
            line = f"{marker} {name}: {self.values[name]}"
            body.append(self._theme.fg("accent" if index == self._selected else "text", line))
        if self._notice:
            body.append(_hint(self._notice, self._theme))
        body.extend(("", _hint("↑/↓ select · ←/→ preview · Enter apply · S save · Esc rollback", self._theme)))
        return self._panel("Appearance", body, width)

    def wants_fullscreen(self, width: int, height: int) -> bool:
        return len(self.render(width)) > height

    def viewport(self, width: int, height: int) -> list[str]:
        capacity = max(0, height - 2)
        avail = _body_width(width)
        if capacity == 0:
            return []
        if self._save_scope:
            if avail <= 16:
                marker = self._theme.symbol("nav.cursor")
                body = (
                    self._theme.fg("accent", f"{marker} {self._scope}"),
                    _hint("←→ choose", self._theme),
                    _hint("Enter save", self._theme),
                    _hint("Esc discard", self._theme),
                )
                return self._panel("Save Appearance", body[:capacity], width)
            return super().viewport(width, height)
        fields = tuple(self._fields)
        if not fields:
            return self._panel("Appearance", [], width)
        notice_rows = [_hint(self._notice, self._theme)] if self._notice else []
        if avail <= 16 or len(self.render(width)) > height:
            footer = (
                _hint("↑↓ select ←→", self._theme),
                _hint("Enter apply", self._theme),
                _hint("S save Esc back", self._theme),
            )
        else:
            footer = (_hint("↑/↓ select · ←/→ preview · Enter apply · S save · Esc rollback", self._theme),)
        field_slots = max(0, capacity - len(footer) - len(notice_rows))
        start = max(0, min(self._selected, len(fields) - field_slots))
        body = []
        for index in range(start, min(len(fields), start + field_slots)):
            name = fields[index]
            marker = self._theme.symbol("nav.cursor") if index == self._selected else " "
            body.append(self._theme.fg("accent" if index == self._selected else "text", f"{marker} {name}: {self.values[name]}"))
        body.extend(notice_rows)
        body.extend(footer)
        return self._panel("Appearance", body[:capacity], width)


class ToolArtifactOverlay(_Base):
    """Bounded, read-only page of a runtime-authorized tool artifact."""

    __slots__ = ("_content", "_has_next", "_has_previous", "_page", "_title")

    def __init__(
        self,
        *,
        title: str,
        content: str,
        page: int,
        has_previous: bool,
        has_next: bool,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._title = title
        self._content = content
        self._page = page
        self._has_previous = has_previous
        self._has_next = has_next

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name == "pageDown" and self._has_next:
            return _done("next")
        if key.name == "pageUp" and self._has_previous:
            return _done("previous")
        return _PENDING

    def render(self, width: int) -> Sequence[str]:
        avail = _body_width(width)
        body: list[str] = []
        for line in self._content.splitlines() or [""]:
            body.extend(self._wrap(line, avail))
        navigation = []
        if self._has_previous:
            navigation.append("PageUp previous")
        if self._has_next:
            navigation.append("PageDown next")
        navigation.append("Esc close")
        body.extend(("", _hint(f"Page {self._page} · " + " · ".join(navigation), self._theme)))
        return self._panel(self._title, body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return True

    def viewport(self, width: int, height: int) -> list[str]:
        if _body_width(width) > 16:
            return super().viewport(width, height)
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        footer = []
        if self._has_previous:
            footer.append(_hint("PageUp previous", self._theme))
        if self._has_next:
            footer.append(_hint("PageDown next", self._theme))
        footer.append(_hint("Esc close", self._theme))
        content: list[str] = []
        for line in self._content.splitlines() or [""]:
            content.extend(self._wrap(line, _body_width(width)))
        body = [*content[: max(0, capacity - len(footer))], *footer]
        return self._panel(self._title, body[:capacity], width)


class ReviewPresetPickerOverlay(_Base):
    """Review preset selection (mirroring upstream Codex 4-item picker).

    1. Review against a base branch (PR Style) -> base_branch
    2. Review uncommitted changes -> uncommitted
    3. Review a commit -> commit
    4. Custom review instructions -> custom
    """

    __slots__ = ("_highlight", "_options")

    OPTIONS: tuple[tuple[str, str, str], ...] = (
        ("base_branch", "Review against a base branch", "(PR Style)"),
        ("uncommitted", "Review uncommitted changes", ""),
        ("commit", "Review a commit", ""),
        ("custom", "Custom review instructions", ""),
    )

    def __init__(self, *, theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._highlight = 0
        self._options = self.OPTIONS

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name in {"up", "k"}:
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name in {"down", "j"}:
            self._highlight = min(len(self._options) - 1, self._highlight + 1)
            return _PENDING
        if key.name == "enter":
            return _done(self._options[self._highlight][0])
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        cursor = theme.symbol("nav.cursor")
        body: list[str] = []
        for index, (_, name, desc) in enumerate(self._options):
            pointer = cursor if index == self._highlight else " "
            label = f"{pointer} {name}"
            if desc:
                label += f" {desc}"
            styled = self._bold("accent", label) if index == self._highlight else theme.fg("text", label)
            body.extend(self._wrap(styled, avail))
        body.append("")
        body.append(_hint("↑/↓ move · Enter select · Esc cancel", theme))
        return self._panel("Select a review preset", body, width)

    def viewport(self, width: int, height: int) -> list[str]:  # noqa: ARG002
        return self.render(width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return False


class ReviewBranchPickerOverlay(_Base):
    """Select a base branch for review."""

    __slots__ = ("_branches", "_current_branch", "_filter", "_highlight")

    def __init__(
        self,
        *,
        branches: Sequence[str],
        current_branch: str | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._branches = tuple(branches)
        self._current_branch = current_branch or "(detached HEAD)"
        self._filter = ""
        self._highlight = 0

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._filter))))

    def _matches(self) -> list[str]:
        tokens = self._filter.lower().split()
        if not tokens:
            return list(self._branches)
        return [b for b in self._branches if all(token in b.lower() for token in tokens)]

    def handle_key(self, key: Key) -> OverlayOutcome:
        matches = self._matches()
        if key.name == "escape":
            return _CANCELLED
        if key.name in {"up", "k"}:
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name in {"down", "j"}:
            self._highlight = min(max(0, len(matches) - 1), self._highlight + 1)
            return _PENDING
        if key.name == "backspace":
            self._filter = self._filter[:-1]
            self._highlight = 0
            return _PENDING
        if key.name == "enter":
            if matches:
                selected = matches[min(self._highlight, len(matches) - 1)]
                return _done(selected)
            return _CANCELLED
        text = _typed_text(key)
        if text:
            self._filter += text
            self._highlight = 0
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        cursor = theme.symbol("nav.cursor")
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._filter))]
        body.append("")
        if not self._branches:
            body.append(theme.fg("warning", "No branches found."))
        elif not matches:
            body.append(theme.fg("dim", "No matching branches."))
        else:
            for index, branch in enumerate(matches):
                pointer = cursor if index == self._highlight else " "
                label = f"{pointer} {self._current_branch} -> {branch}"
                styled = self._bold("accent", label) if index == self._highlight else theme.fg("text", label)
                body.extend(self._wrap(styled, avail))
        body.append("")
        body.append(_hint("↑/↓ move · Enter select · type to filter · Esc cancel", theme))
        return self._panel("Select a base branch", body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return True

    def viewport(self, width: int, height: int) -> list[str]:
        if _body_width(width) > 16 and len(self.render(width)) <= height:
            return super().viewport(width, height)
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        body = [self._theme.fg("accent", "> " + escape_terminal_controls(self._filter))]
        if matches:
            branch = matches[self._highlight]
            pointer = self._theme.symbol("nav.cursor")
            body.append(self._bold("accent", f"{pointer} {self._current_branch} -> {branch}"))
        elif self._branches:
            body.append(self._theme.fg("dim", "No matching branches."))
        else:
            body.append(self._theme.fg("warning", "No branches found."))
        body.extend((_hint("↑↓ Enter select", self._theme), _hint("Esc cancel·type", self._theme)))
        return self._panel("Select a base branch", body[:capacity], width)


class ReviewCommitPickerOverlay(_Base):
    """Select a commit to review."""

    __slots__ = ("_commits", "_filter", "_highlight")

    def __init__(
        self,
        *,
        commits: Sequence[ReviewCommitLogEntry],
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._commits = tuple(commits)
        self._filter = ""
        self._highlight = 0

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._filter))))

    def _matches(self) -> list[ReviewCommitLogEntry]:
        tokens = self._filter.lower().split()
        if not tokens:
            return list(self._commits)
        matched: list[ReviewCommitLogEntry] = []
        for commit in self._commits:
            haystack = f"{commit.subject} {commit.sha}".lower()
            if all(token in haystack for token in tokens):
                matched.append(commit)
        return matched

    def handle_key(self, key: Key) -> OverlayOutcome:
        matches = self._matches()
        if key.name == "escape":
            return _CANCELLED
        if key.name in {"up", "k"}:
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name in {"down", "j"}:
            self._highlight = min(max(0, len(matches) - 1), self._highlight + 1)
            return _PENDING
        if key.name == "backspace":
            self._filter = self._filter[:-1]
            self._highlight = 0
            return _PENDING
        if key.name == "enter":
            if matches:
                selected = matches[min(self._highlight, len(matches) - 1)]
                return _done(selected)
            return _CANCELLED
        text = _typed_text(key)
        if text:
            self._filter += text
            self._highlight = 0
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        cursor = theme.symbol("nav.cursor")
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._filter))]
        body.append("")
        if not self._commits:
            body.append(theme.fg("warning", "No commits found."))
        elif not matches:
            body.append(theme.fg("dim", "No matching commits."))
        else:
            for index, commit in enumerate(matches):
                pointer = cursor if index == self._highlight else " "
                short_sha = commit.sha[:7]
                label = f"{pointer} {commit.subject} ({short_sha})"
                styled = self._bold("accent", label) if index == self._highlight else theme.fg("text", label)
                body.extend(self._wrap(styled, avail))
        body.append("")
        body.append(_hint("↑/↓ move · Enter select · type to filter · Esc cancel", theme))
        return self._panel("Select a commit to review", body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return True

    def viewport(self, width: int, height: int) -> list[str]:
        if _body_width(width) > 16 and len(self.render(width)) <= height:
            return super().viewport(width, height)
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        matches = self._matches()
        self._highlight = min(self._highlight, max(0, len(matches) - 1))
        body = [self._theme.fg("accent", "> " + escape_terminal_controls(self._filter))]
        if matches:
            commit = matches[self._highlight]
            pointer = self._theme.symbol("nav.cursor")
            body.append(self._bold("accent", f"{pointer} {commit.subject} ({commit.sha[:7]})"))
        elif self._commits:
            body.append(self._theme.fg("dim", "No matching commits."))
        else:
            body.append(self._theme.fg("warning", "No commits found."))
        body.extend((_hint("↑↓ Enter select", self._theme), _hint("Esc cancel·type", self._theme)))
        return self._panel("Select a commit to review", body[:capacity], width)


class ReviewCustomPromptOverlay(_Base):
    """Custom review instructions prompt."""

    __slots__ = ("_text",)

    def __init__(self, *, theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._text = ""

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._text))))

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name == "backspace":
            self._text = self._text[:-1]
            return _PENDING
        if key.name == "enter":
            trimmed = self._text.strip()
            if trimmed:
                return _done(trimmed)
            return _CANCELLED
        text = _typed_text(key)
        if text:
            self._text += text
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        body = [
            theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._text)),
            "",
            _hint("Type instructions and press Enter · Esc cancel", theme),
        ]
        return self._panel("Custom review instructions", body, width)

    def viewport(self, width: int, height: int) -> list[str]:  # noqa: ARG002
        return self.render(width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return False


class SideQuestionOverlay(_Base):
    """Read-only ``/btw`` answer view: scroll with ↑/↓, dismiss with Space/Enter/Esc."""

    __slots__ = ("_answer", "_question", "_scroll")

    def __init__(self, *, question: str, answer: str, theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._question = question
        self._answer = answer
        self._scroll = 0

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name in {"escape", "enter", "space"}:
            return _CANCELLED
        if key.name in {"up", "down", "pageUp", "pageDown"}:
            delta = -1 if key.name in {"up", "pageUp"} else 1
            self._scroll = max(0, self._scroll + delta * (10 if key.name.startswith("page") else 1))
        return _PENDING

    def render(self, width: int) -> Sequence[str]:
        avail = _body_width(width)
        body: list[str] = [self._bold("warning", "/btw ") + self._fg("dim", self._question)]
        body.append("")
        for line in self._answer.splitlines() or [""]:
            body.extend(self._wrap(self._fg("text", line), avail))
        body.extend(("", _hint("↑/↓ scroll · Space, Enter, or Esc dismiss", self._theme)))
        return self._panel("Side question", body, width)

    def wants_fullscreen(self, width: int, height: int) -> bool:
        return len(self.render(width)) > height

    def viewport(self, width: int, height: int) -> list[str]:
        capacity = max(0, height - 2)
        if capacity == 0:
            return []
        avail = _body_width(width)
        header = [self._bold("warning", "/btw ") + self._fg("dim", self._question), ""]
        content: list[str] = []
        for line in self._answer.splitlines() or [""]:
            content.extend(self._wrap(self._fg("text", line), avail))
        footer = _hint("↑/↓ scroll · Space/Enter/Esc dismiss", self._theme)
        scroll_slots = max(0, capacity - len(header) - 1)
        self._scroll = min(self._scroll, max(0, len(content) - scroll_slots))
        body = [*header, *content[self._scroll : self._scroll + scroll_slots], footer]
        return self._panel("Side question", body[:capacity], width)


class RenameSessionOverlay(_Base):
    """Session rename prompt (mirroring upstream Codex CustomPromptView)."""

    __slots__ = ("_text",)

    def __init__(self, *, initial_text: str = "", theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._text = initial_text

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._text))))

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name == "backspace":
            self._text = self._text[:-1]
            return _PENDING
        if key.name == "enter":
            return _done(self._text)
        text = _typed_text(key)
        if text:
            self._text += text
        return _PENDING

    def render(self, width: int) -> list[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        title = "Rename thread" if self._text else "Name thread"
        body = [
            theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._text)),
            "",
            _hint("Type a name and press Enter · Esc cancel", theme),
        ]
        return self._panel(title, body, width)

    def viewport(self, width: int, height: int) -> list[str]:  # noqa: ARG002
        return self.render(width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return False


class _ChoicePickerOverlay(_Base):
    """Up/down + enter choice list (review-picker grammar, arbitrary rows)."""

    __slots__ = ("_highlight", "_options", "_title")

    def __init__(self, *, title: str, options: tuple[tuple[str, str], ...], theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._title = title
        self._options = options
        self._highlight = 0

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name in {"up", "k"}:
            self._highlight = max(0, self._highlight - 1)
            return _PENDING
        if key.name in {"down", "j"}:
            self._highlight = min(len(self._options) - 1, self._highlight + 1)
            return _PENDING
        if key.name == "enter":
            return _done(self._options[self._highlight][0])
        return _PENDING

    def render(self, width: int) -> Sequence[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        cursor = theme.symbol("nav.cursor")
        body: list[str] = []
        for index, (_, label) in enumerate(self._options):
            pointer = cursor if index == self._highlight else " "
            row = f"{pointer} {label}"
            styled = self._bold("accent", row) if index == self._highlight else theme.fg("text", row)
            body.extend(self._wrap(styled, avail))
        body.append("")
        body.append(_hint("↑/↓ move · Enter select · Esc cancel", theme))
        return self._panel(self._title, body, width)

    def viewport(self, width: int, height: int) -> list[str]:  # noqa: ARG002
        return list(self.render(width))

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return False


class CopyPickerOverlay(_ChoicePickerOverlay):
    """``/copy`` target picker: whole response + code blocks + quotes."""

    def __init__(self, *, options: tuple[tuple[str, str], ...], theme: Theme | None = None) -> None:
        super().__init__(title="Copy from response", options=options, theme=theme)


class ExportPickerOverlay(_ChoicePickerOverlay):
    """``/export`` destination picker: clipboard or file."""

    OPTIONS: tuple[tuple[str, str], ...] = (
        ("clipboard", "Copy to clipboard — full Markdown transcript"),
        ("file", "Save to file — choose a Markdown filename"),
    )

    def __init__(self, *, theme: Theme | None = None) -> None:
        super().__init__(title="Export conversation", options=self.OPTIONS, theme=theme)


class ExportPathOverlay(_Base):
    """``/export`` filename prompt (Codex transcript-export file prompt parity)."""

    __slots__ = ("_text",)

    def __init__(self, *, initial_text: str = "", theme: Theme | None = None) -> None:
        super().__init__(theme)
        self._text = initial_text

    def cursor_position(self, width: int) -> tuple[int, int] | None:
        return (1, min(max(0, width - 1), 4 + visible_width(escape_terminal_controls(self._text))))

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name == "escape":
            return _CANCELLED
        if key.name == "backspace":
            self._text = self._text[:-1]
            return _PENDING
        if key.name == "enter":
            trimmed = self._text.strip()
            return _done(trimmed) if trimmed else _CANCELLED
        text = _typed_text(key)
        if text:
            self._text += text
        return _PENDING

    def render(self, width: int) -> Sequence[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        body = [
            theme.fg("accent", "> ") + theme.fg("text", escape_terminal_controls(self._text)),
            "",
            _hint("Type a filename and press Enter · Esc cancel", theme),
        ]
        return self._panel("Save conversation", body, width)

    def viewport(self, width: int, height: int) -> list[str]:  # noqa: ARG002
        return list(self.render(width))

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return False


class ModelPickerOverlay(_ChoicePickerOverlay):
    """``/model`` selection popup listing available models and reasoning options."""

    def __init__(
        self,
        *,
        options: tuple[tuple[str, str], ...],
        current_model: str | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(title="Select model", options=options, theme=theme)
        if current_model:
            for idx, (val, _) in enumerate(options):
                if val == current_model or val.endswith(f"/{current_model}"):
                    self._highlight = idx
                    break


class PermissionsPickerOverlay(_ChoicePickerOverlay):
    """``/permissions`` mode selection popup with Codex approval presets."""

    PRESETS: tuple[tuple[str, str], ...] = (
        ("read-only", "Read Only — Codex can read workspace files; approval required for edits and commands"),
        ("auto", "Default — Codex can read and edit workspace files; approval required for commands"),
        ("full-access", "Full Access — Codex can edit files and run commands without asking"),
    )

    def __init__(
        self,
        *,
        current_preset: str | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(title="Permissions & Approval Mode", options=self.PRESETS, theme=theme)
        if current_preset:
            for idx, (val, _) in enumerate(self.PRESETS):
                if val == current_preset:
                    self._highlight = idx
                    break


class ApprovePickerOverlay(_ChoicePickerOverlay):
    """``/approve`` selection popup for pending approval requests."""

    def __init__(
        self,
        *,
        options: tuple[tuple[str, str], ...],
        theme: Theme | None = None,
    ) -> None:
        super().__init__(title="Auto-review Denials & Pending Approvals", options=options, theme=theme)


@dataclass(slots=True)
class SkillsToggleItem:
    name: str
    description: str
    origin: str
    enabled: bool


class SkillsMenuOverlay(_ChoicePickerOverlay):
    """``/skills`` top-level choice menu mirroring upstream Codex."""

    OPTIONS: tuple[tuple[str, str], ...] = (
        ("list", "List skills — Tip: press $ to open this list directly"),
        ("manage", "Enable/Disable Skills — Enable or disable skills"),
    )

    def __init__(self, *, theme: Theme | None = None) -> None:
        super().__init__(title="Skills", options=self.OPTIONS, theme=theme)


class SkillsManageOverlay(_Base):
    """``/skills`` manage popup with per-skill toggles (Codex parity)."""

    def __init__(
        self,
        *,
        items: Sequence[SkillsToggleItem],
        on_toggle: Callable[[str, bool], None] | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._items = list(items)
        self._on_toggle = on_toggle
        self._highlight = 0
        self._initial_state: dict[str, bool] = {item.name: item.enabled for item in items}

    def handle_key(self, key: Key) -> OverlayOutcome:
        if key.name in {"escape", "ctrl+c"}:
            enabled_count = 0
            disabled_count = 0
            for item in self._items:
                was_enabled = self._initial_state.get(item.name)
                if was_enabled is not None and was_enabled != item.enabled:
                    if item.enabled:
                        enabled_count += 1
                    else:
                        disabled_count += 1
            return _done((enabled_count, disabled_count))
        if key.name in {"up", "k"}:
            if self._items:
                self._highlight = (self._highlight - 1) % len(self._items)
            return _PENDING
        if key.name in {"down", "j"}:
            if self._items:
                self._highlight = (self._highlight + 1) % len(self._items)
            return _PENDING
        if key.name in {"space", "enter"}:
            if self._items and 0 <= self._highlight < len(self._items):
                item = self._items[self._highlight]
                item.enabled = not item.enabled
                if self._on_toggle is not None:
                    self._on_toggle(item.name, item.enabled)
            return _PENDING
        return _PENDING

    def render(self, width: int) -> Sequence[str]:
        avail = _body_width(width)
        if avail <= 0:
            return []
        theme = self._theme
        cursor = theme.symbol("nav.cursor")
        body: list[str] = [
            theme.fg("muted", "Turn skills on or off. Your changes are saved automatically."),
            "",
        ]
        if not self._items:
            body.append(theme.fg("muted", "No skills available."))
        else:
            for index, item in enumerate(self._items):
                pointer = cursor if index == self._highlight else " "
                marker = "x" if item.enabled else " "
                name_line = f"{pointer} [{marker}] {item.name} ({item.origin})"
                styled_name = self._bold("accent", name_line) if index == self._highlight else theme.fg("text", name_line)
                body.extend(self._wrap(styled_name, avail))
                if item.description:
                    desc_line = f"    {item.description}"
                    body.extend(self._wrap(theme.fg("muted", desc_line), avail))
        body.append("")
        body.append(_hint("↑/↓ move · Space/Enter toggle · Esc close", theme))
        return self._panel("Enable/Disable Skills", body, width)

    def viewport(self, width: int, height: int) -> list[str]:  # noqa: ARG002
        return list(self.render(width))

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return False
