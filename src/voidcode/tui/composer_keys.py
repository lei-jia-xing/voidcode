"""Key handlers for the composer: navigation, deletion, submission, and paste chords."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .composer_text import (
    _bounds_in,
    _cursor_down_in,
    _cursor_up_in,
    _next_boundary,
    _previous_boundary,
    _snap_left,
    _snap_right,
    _word_left,
    _word_right,
)
from .composer_types import DOUBLE_INTERRUPT_SECONDS, ComposerAction, ComposerOutcome
from .keys import Key

if TYPE_CHECKING:
    from .composer import Composer


class ComposerKeys:
    """The imperative half of the keymap; ``Composer._KEYMAP`` binds these."""

    def _key_escape(self: Composer, _key: Key) -> ComposerOutcome:
        if self._completion:
            # The 50 ms lone-Esc disambiguation already ran in the app loop:
            # by the time this key arrives it is a real Esc, so cancel the
            # popup without cancelling the turn.
            self.set_completion(())
            return ComposerOutcome()
        return ComposerOutcome(ComposerAction.CANCEL_TURN)

    def _key_ctrl_c(self: Composer, _key: Key) -> ComposerOutcome:
        now = self._clock()
        if now - self._last_interrupt < DOUBLE_INTERRUPT_SECONDS:
            self._last_interrupt = now
            return ComposerOutcome(ComposerAction.INTERRUPT)
        self.clear()
        self._last_interrupt = now
        return ComposerOutcome()

    def _key_tab(self: Composer, _key: Key) -> ComposerOutcome:
        """Tab: accept the highlighted candidate (arrows move first)."""
        if not self._completion:
            return ComposerOutcome()
        self._accept_completion()
        return ComposerOutcome()

    def _key_newline(self: Composer, _key: Key) -> ComposerOutcome:
        self._insert("\n")
        return ComposerOutcome()

    def _key_enter(self: Composer, _key: Key) -> ComposerOutcome:
        if self._completion:
            self._accept_completion()
            return ComposerOutcome()
        return self._submit()

    def _key_paste(self: Composer, key: Key) -> ComposerOutcome:
        self._insert_paste(key.text)
        return ComposerOutcome()

    def _key_expand(self: Composer, _key: Key) -> ComposerOutcome:
        self._toggle_block_at_cursor()
        return ComposerOutcome()

    def _key_copy(self: Composer, _key: Key) -> ComposerOutcome:
        copied = self._copy_block_at_cursor()
        if copied is None:
            return ComposerOutcome()
        return ComposerOutcome(ComposerAction.COPY, copied)

    def _key_delete_block(self: Composer, _key: Key) -> ComposerOutcome:
        self._delete_block_at_cursor()
        return ComposerOutcome()

    def _key_left(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        for block in collapsed:
            if self._cursor == block.end:
                self._cursor = block.start
                return ComposerOutcome()
        target = _previous_boundary(self._text, self._cursor)
        for block in collapsed:
            if block.start < target < block.end:
                target = block.start
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_right(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        for block in collapsed:
            if self._cursor == block.start:
                self._cursor = block.end
                return ComposerOutcome()
        target = _next_boundary(self._text, self._cursor)
        for block in collapsed:
            if block.start < target < block.end:
                target = block.end
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_up(self: Composer, _key: Key) -> ComposerOutcome:
        if self._completion:
            self._completion_index = (self._completion_index - 1) % len(self._completion)
            return ComposerOutcome()
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            display_start, _ = _bounds_in(display_text, display_cursor)
            if self._history_index is not None or display_start == 0:
                self._history_up()
            else:
                old_true = self._cursor
                new_display = _cursor_up_in(display_text, display_cursor)
                self._cursor = self._display_to_true(new_display, old_true)
            return ComposerOutcome()
        if self._history_index is not None or self._bounds()[0] == 0:
            self._history_up()
        else:
            self._cursor_up()
        return ComposerOutcome()

    def _key_down(self: Composer, _key: Key) -> ComposerOutcome:
        if self._completion:
            self._completion_index = (self._completion_index + 1) % len(self._completion)
            return ComposerOutcome()
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            _, display_end = _bounds_in(display_text, display_cursor)
            if self._history_index is not None or display_end == len(display_text):
                self._history_down()
            else:
                old_true = self._cursor
                new_display = _cursor_down_in(display_text, display_cursor)
                self._cursor = self._display_to_true(new_display, old_true)
            return ComposerOutcome()
        if self._history_index is not None or self._bounds()[1] == len(self._text):
            self._history_down()
        else:
            self._cursor_down()
        return ComposerOutcome()

    def _key_home(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            display_start, _ = _bounds_in(display_text, display_cursor)
            self._cursor = self._display_to_true(display_start, self._cursor)
        else:
            start, _ = self._bounds()
            self._cursor = start
        return ComposerOutcome()

    def _key_end(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            _, display_end = _bounds_in(display_text, display_cursor)
            self._cursor = self._display_to_true(display_end, self._cursor)
        else:
            _, end = self._bounds()
            self._cursor = end
        return ComposerOutcome()

    def _key_word_left(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        target = _snap_left(self._text, _word_left(self._text, self._cursor))
        for block in collapsed:
            if block.start < target < block.end:
                target = block.start
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_word_right(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        target = _snap_right(self._text, _word_right(self._text, self._cursor))
        for block in collapsed:
            if block.start < target < block.end:
                target = block.end
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_backspace(self: Composer, _key: Key) -> ComposerOutcome:
        if self._cursor > 0:
            target = _previous_boundary(self._text, self._cursor)
            self._delete_true_range(target, self._cursor)
        return ComposerOutcome()

    def _key_delete(self: Composer, _key: Key) -> ComposerOutcome:
        if self._cursor < len(self._text):
            target = _next_boundary(self._text, self._cursor)
            self._delete_true_range(self._cursor, target)
        return ComposerOutcome()

    def _key_delete_word_back(self: Composer, _key: Key) -> ComposerOutcome:
        target = _snap_left(self._text, _word_left(self._text, self._cursor))
        if target != self._cursor:
            self._delete_true_range(target, self._cursor)
        return ComposerOutcome()

    def _key_delete_word_forward(self: Composer, _key: Key) -> ComposerOutcome:
        target = _snap_right(self._text, _word_right(self._text, self._cursor))
        if target != self._cursor:
            self._delete_true_range(self._cursor, target)
        return ComposerOutcome()

    def _key_ctrl_u(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            display_start, _ = _bounds_in(display_text, display_cursor)
            if display_cursor > display_start:
                old_true = self._cursor
                true_start = self._display_to_true(display_start, old_true)
                true_end = self._display_to_true(display_cursor, old_true)
                if true_end > true_start:
                    self._delete_true_range(true_start, true_end)
                else:
                    self._cursor = true_start
            return ComposerOutcome()
        start, _ = self._bounds()
        if self._cursor > start:
            self._text = self._text[:start] + self._text[self._cursor :]
            self._cursor = start
            self._edited()
        return ComposerOutcome()

    def _key_ctrl_k(self: Composer, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            _, display_end = _bounds_in(display_text, display_cursor)
            if display_cursor < display_end:
                old_true = self._cursor
                true_start = self._display_to_true(display_cursor, old_true)
                true_end = self._display_to_true(display_end, old_true)
                if true_end > true_start:
                    self._delete_true_range(true_start, true_end)
            return ComposerOutcome()
        _, end = self._bounds()
        if self._cursor < end:
            self._text = self._text[: self._cursor] + self._text[end:]
            self._edited()
        return ComposerOutcome()
