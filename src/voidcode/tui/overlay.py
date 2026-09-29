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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .keys import Key
from .term import clamp_row, visible_width, wrap_row
from .theme import Theme, resolve_theme
from .transcript import short_session_id

__all__ = [
    "ApprovalOverlay",
    "Overlay",
    "OverlayOutcome",
    "OverlayOutcomeKind",
    "PromptHistoryOverlay",
    "QuestionOverlay",
    "SessionPickerOverlay",
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

    def handle_key(self, key: Key, /) -> OverlayOutcome:
        """Consume one decoded key."""
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

    __slots__ = ("_arguments", "_reason", "_target", "_tool")

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
        if key.name == "y":
            return _done("allow")
        if key.name in ("n", "escape"):
            return _done("deny")
        return _PENDING

    def wants_fullscreen(self, width: int, height: int) -> bool:
        return len(self.render(width)) > height


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

    __slots__ = ("_answers", "_highlight", "_page_index", "_questions")

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
        self._highlight = 0

    # -- state -------------------------------------------------------------

    @property
    def _on_review(self) -> bool:
        return self._page_index >= len(self._questions)

    def _answer_values(self, index: int) -> tuple[str, ...]:
        answer = self._answers[index]
        return tuple(option.label for option in self._questions[index].options if option.label in answer.selected)

    def _reset_view(self) -> None:
        self._highlight = 0

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
        if key.name == "left":
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

    ``wants_fullscreen`` is always ``True``: omp's session selector and the
    ``--resume`` picker are fullscreen overlays (``session-selector.ts``;
    plan ``.omo/plans/tui-omp-research.md`` phase 4).

    ``depths`` maps a session id to its fork-forest depth; a row indents two
    spaces per level (the CLI's ``sessions tree`` convention) and a missing id
    renders at depth 0. Indentation is visual only -- the filter haystack is
    the bare id/title.
    """

    __slots__ = ("_depths", "_filter", "_highlight", "_sessions")

    def __init__(
        self,
        *,
        sessions: Sequence[tuple[str, str]],
        depths: Mapping[str, int] | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__(theme)
        self._sessions = tuple(sessions)
        # Fork depth per session id (``VoidCodeRuntime.session_forest``), display
        # only: a session absent from the map renders at depth 0.
        self._depths = dict(depths) if depths is not None else {}
        self._filter = ""
        self._highlight = 0

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
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", self._filter) + theme.fg("dim", "▌")]
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
        body.append(_hint("↑/↓ move · Enter resume · type to filter · Esc cancel", theme))
        return self._panel("Select Session", body, width)

    def wants_fullscreen(self, _width: int, _height: int) -> bool:
        return True


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
        body: list[str] = [theme.fg("accent", "> ") + theme.fg("text", self._filter) + theme.fg("dim", "▌")]
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
