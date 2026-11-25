"""Composer editing primitives: cursor movement, insertion/deletion, paste folding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .composer_rendering import _FRAMES
from .composer_text import _badge_for_text, _bounds_in, _cursor_down_in, _cursor_up_in, _PasteBlock, _wrap_display_line
from .term import visible_width

if TYPE_CHECKING:
    from .composer import Composer


class ComposerEditing:
    """Draft mutation, paste-block bookkeeping, and true/display offset mapping."""

    def _layout_width(self: Composer) -> int:
        """Usable input cells after frame inset and prompt gutter."""
        frame = _FRAMES[self._style]
        inset = int(frame.sides) * 2 + int(frame.sidebar) * 2 + int(frame.rail)
        width = max(1, self._width - inset)
        gutter = self._prompt_gutter()
        gutter_width = min(visible_width(gutter), max(0, width - 2))
        return max(2, width - gutter_width)

    def _count_display_rows(self: Composer, text: str, layout_width: int) -> int:
        """Wrapped row count for ``text`` at ``layout_width`` (pure viewport math)."""
        total = 0
        for line in text.split("\n"):
            rows, _ = _wrap_display_line(line, layout_width)
            total += len(rows)
        return total

    def _should_fold_paste(self: Composer, paste_text: str) -> bool:
        """Fold only a multi-line paste taller than the visible input height."""
        if "\n" not in paste_text:
            return False
        visible = max(1, self._max_height)
        if paste_text.count("\n") + 1 > visible:
            return True
        return self._count_display_rows(paste_text, self._layout_width()) > visible

    def _collapsed_blocks(self: Composer) -> list[_PasteBlock]:
        """Collapsed blocks in true-offset order (the list is always sorted)."""
        return [block for block in self._paste_blocks if block.collapsed]

    def _display_text_and_cursor(self: Composer) -> tuple[str, int]:
        """Display text with badges plus the display offset for the true cursor."""
        collapsed = self._collapsed_blocks()
        if not collapsed:
            return self._text, self._cursor
        parts: list[str] = []
        display_cursor: int | None = None
        display_pos = 0
        prev_true = 0
        for block in collapsed:
            run = self._text[prev_true : block.start]
            if display_cursor is None and prev_true <= self._cursor <= block.start:
                display_cursor = display_pos + (self._cursor - prev_true)
            parts.append(run)
            display_pos += len(run)
            badge = _badge_for_text(self._text[block.start : block.end])
            if display_cursor is None and block.start < self._cursor < block.end:
                display_cursor = display_pos + len(badge)
            parts.append(badge)
            display_pos += len(badge)
            prev_true = block.end
        tail = self._text[prev_true:]
        if display_cursor is None and prev_true <= self._cursor <= len(self._text):
            display_cursor = display_pos + (self._cursor - prev_true)
        parts.append(tail)
        if display_cursor is None:
            display_cursor = display_pos + len(tail)
        return "".join(parts), display_cursor

    def _display_to_true(self: Composer, display_offset: int, old_true: int) -> int:
        """Map a display offset back to true text; inside a badge snap to an edge."""
        collapsed = self._collapsed_blocks()
        if not collapsed:
            return max(0, min(display_offset, len(self._text)))
        display_pos = 0
        prev_true = 0
        for block in collapsed:
            run_len = block.start - prev_true
            if display_offset <= display_pos + run_len:
                return prev_true + (display_offset - display_pos)
            display_pos += run_len
            badge_len = len(_badge_for_text(self._text[block.start : block.end]))
            if display_offset <= display_pos + badge_len:
                if display_offset == display_pos:
                    return block.start
                if display_offset == display_pos + badge_len:
                    return block.end
                return block.start if old_true <= block.start else block.end
            display_pos += badge_len
            prev_true = block.end
        tail_len = len(self._text) - prev_true
        if display_offset <= display_pos + tail_len:
            return prev_true + (display_offset - display_pos)
        return len(self._text)

    def _block_at_cursor(self: Composer) -> _PasteBlock | None:
        """Block under the caret; adjacent collapsed wins over expanded."""
        matching = [block for block in self._paste_blocks if block.start <= self._cursor <= block.end]
        if not matching:
            return None
        if len(matching) == 1:
            return matching[0]
        for block in matching:
            if block.collapsed:
                return block
        return matching[0]

    def _adjust_blocks_for_insert(self: Composer, pos: int, length: int) -> None:
        """Shift or grow spans for an insertion of ``length`` at ``pos``."""
        if length <= 0:
            return
        for block in self._paste_blocks:
            if pos <= block.start:
                block.start += length
                block.end += length
            elif pos < block.end:
                block.end += length

    def _adjust_blocks_for_delete(self: Composer, del_start: int, del_end: int) -> None:
        """Drop, truncate, or shift spans for the removed true range."""
        deleted = del_end - del_start
        if deleted <= 0:
            return
        kept: list[_PasteBlock] = []
        for block in self._paste_blocks:
            if block.end <= del_start:
                kept.append(block)
            elif block.start >= del_end:
                block.start -= deleted
                block.end -= deleted
                kept.append(block)
            elif del_start <= block.start and block.end <= del_end:
                continue
            elif block.start < del_start and del_end < block.end:
                block.end -= deleted
                kept.append(block)
            elif del_start <= block.start < del_end < block.end:
                remaining = block.end - del_end
                block.start = del_start
                block.end = del_start + remaining
                kept.append(block)
            elif block.start < del_start < block.end <= del_end:
                block.end = del_start
                kept.append(block)
            else:
                kept.append(block)
        self._paste_blocks = [block for block in kept if block.start < block.end]

    def _expand_delete_for_collapsed(self: Composer, del_start: int, del_end: int) -> tuple[int, int]:
        """Grow a true delete range to include every touched collapsed block."""
        if del_start >= del_end:
            return del_start, del_end
        new_start, new_end = del_start, del_end
        for block in self._paste_blocks:
            if not block.collapsed:
                continue
            if new_start < block.end and new_end > block.start:
                new_start = min(new_start, block.start)
                new_end = max(new_end, block.end)
        return new_start, new_end

    def _delete_true_range(self: Composer, del_start: int, del_end: int) -> None:
        """Delete one true range, collapsing atomic badges it touches."""
        if del_start >= del_end:
            return
        del_start, del_end = self._expand_delete_for_collapsed(del_start, del_end)
        self._text = self._text[:del_start] + self._text[del_end:]
        self._cursor = del_start
        self._adjust_blocks_for_delete(del_start, del_end)
        self._edited()

    def _insert_paste(self: Composer, raw_text: str) -> None:
        """Insert pasted text, folding only a viewport-tall multi-line fragment."""
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        pos = self._cursor
        for block in self._paste_blocks:
            if block.start < pos < block.end:
                self._insert(text)
                return
        should_fold = self._should_fold_paste(text)
        self._adjust_blocks_for_insert(pos, len(text))
        self._text = self._text[:pos] + text + self._text[pos:]
        self._cursor = pos + len(text)
        self._edited()
        if should_fold:
            self._paste_blocks.append(_PasteBlock(pos, pos + len(text), True))
            self._paste_blocks.sort(key=lambda item: item.start)

    def _toggle_block_at_cursor(self: Composer) -> None:
        """Expand a collapsed badge or collapse an expanded block under the caret."""
        block = self._block_at_cursor()
        if block is None:
            return
        if block.collapsed:
            block.collapsed = False
            return
        if block.start < self._cursor < block.end:
            if self._cursor - block.start < block.end - self._cursor:
                self._cursor = block.start
            else:
                self._cursor = block.end
        block.collapsed = True

    def _copy_block_at_cursor(self: Composer) -> str | None:
        """Exact true text of the block under the caret, if any."""
        block = self._block_at_cursor()
        if block is None:
            return None
        return self._text[block.start : block.end]

    def _delete_block_at_cursor(self: Composer) -> None:
        """Delete the whole true span of the block under the caret, if any."""
        block = self._block_at_cursor()
        if block is None:
            return
        self._delete_true_range(block.start, block.end)

    def _insert(self: Composer, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        pos = self._cursor
        self._adjust_blocks_for_insert(pos, len(text))
        self._text = self._text[:pos] + text + self._text[pos:]
        self._cursor = pos + len(text)
        self._edited()

    def _edited(self: Composer) -> None:
        self._history_index = None
        self.set_completion(())

    def _bounds(self: Composer) -> tuple[int, int]:
        """Start and end offsets of the logical line holding the cursor."""
        return _bounds_in(self._text, self._cursor)

    def _cursor_up(self: Composer) -> None:
        self._cursor = _cursor_up_in(self._text, self._cursor)

    def _cursor_down(self: Composer) -> None:
        self._cursor = _cursor_down_in(self._text, self._cursor)
