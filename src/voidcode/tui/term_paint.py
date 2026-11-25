"""Inline live-region painting: committed rows, frames, cursor, and alt screen."""

from __future__ import annotations

import os
from collections.abc import Sequence

from .kitty_graphics import delete_placement, place_image
from .region import KittyImagePlacement, TuiFrame, diff_rows
from .term_ansi import (
    ALTERNATE_SCREEN_OFF,
    ALTERNATE_SCREEN_ON,
    AUTOWRAP_OFF,
    AUTOWRAP_ON,
    CARRIAGE_RETURN,
    ERASE_BELOW,
    ERASE_TO_END_OF_LINE,
    HIDE_CURSOR,
    SHOW_CURSOR,
    SYNC_BEGIN,
    SYNC_END,
)
from .term_text import clamp_row, wrap_row

__all__ = ["LiveRegionPainting"]


class LiveRegionPainting:
    """Terminal mixin owning the inline live region; the terminal supplies I/O."""

    _closed: bool
    _is_tty: bool
    _alt: bool
    _raw: bool
    _resize: bool
    _painted: list[str]
    _painted_width: int
    _painted_cursor: tuple[int, int] | None
    _painted_image: KittyImagePlacement | None
    _private_modes: dict[int, bool | None]
    _kitty_supported: bool
    _kitty_images: dict[str, tuple[int, tuple[int, int]]]
    _kitty_pending_placements: dict[tuple[int, int], str]

    @property
    def width(self) -> int:
        raise NotImplementedError

    @property
    def height(self) -> int:
        raise NotImplementedError

    def _emit(self, text: str) -> None:
        raise NotImplementedError

    def commit_rows(self, rows: Sequence[str]) -> None:
        """Write finalized rows into the native scrollback, exactly once.

        Long rows are ANSI-aware wrapped to the terminal width before writing,
        so a settled command/model/error line can never lose its tail. Rows are
        written top-down and never rewritten. The live region is erased first so
        the scroll a full screen triggers can only push committed rows.
        """
        if self._closed:
            return
        rows = list(rows)
        if not rows:
            return
        if not self._is_tty:
            row_width = max(1, self.width)
            rows = [part for row in rows for part in wrap_row(row, row_width)]
            self._emit("".join(row + "\n" for row in (clamp_row(row, row_width) for row in rows)))
            return
        if self._geometry_stale():
            self._reset_live()
        row_width = self._frame_row_width()
        rows = [part for row in rows for part in wrap_row(row, row_width)]
        parts = [self._paint_begin(), self._cursor_to_live_origin(), self._forget_painted_image()]
        if self._painted:
            parts.append(ERASE_BELOW)
        for row in rows:
            parts.append(clamp_row(row, row_width))
            parts.append(self._newline())
        parts.append(self._paint_end())
        self._emit("".join(parts))
        self._painted = []
        self._painted_cursor = None
        self._painted_width = self.width

    def paint_frame(self, frame: TuiFrame) -> None:
        """Paint one typed frame and place its optional native cursor/image."""
        if self._closed:
            return
        if self._geometry_stale():
            self._reset_live()
        rows = frame.rows
        if not self._is_tty:
            rendered = [clamp_row(row, self.width) for row in rows][-self.height :]
            if rendered == self._painted:
                return
            self._painted = rendered
            self._emit("".join(row + "\n" for row in rendered))
            return

        row_width = self._frame_row_width()
        rendered = [clamp_row(row, row_width) for row in rows]
        split = max(0, len(rendered) - self.height)
        rendered = rendered[split:]
        cursor = frame.cursor
        if cursor is not None:
            row, column = cursor
            cursor = (row - split, min(max(0, column), row_width - 1)) if split <= row < split + len(rendered) else None
        image = frame.image
        if image is not None:
            if split <= image.row < split + len(rendered) and self._kitty_supported and image.sha256 in self._kitty_images:
                image = KittyImagePlacement(image.sha256, image.row - split, image.column, image.columns, image.placement_id)
            else:
                image = None
        previous = self._painted
        changes = diff_rows(previous, rendered)
        shrink = len(previous) > len(rendered)
        graphics_changed = image != self._painted_image
        if not changes and not shrink and cursor == self._painted_cursor and not graphics_changed:
            return
        parts = [self._paint_begin(), self._cursor_to_live_origin()]
        parked_row = 0
        for index, row in changes:
            if index > parked_row:
                parts.append("\n" * (index - parked_row))
            parts.append(CARRIAGE_RETURN + ERASE_TO_END_OF_LINE + row)
            parked_row = index
        if shrink:
            target = len(rendered)
            if target > parked_row:
                parts.append("\n" * (target - parked_row))
            elif target < parked_row:
                parts.append(f"\x1b[{parked_row - target}A")
            parts.append(CARRIAGE_RETURN + ERASE_BELOW)
            parked_row = target
        if graphics_changed:
            if parked_row:
                parts.append(f"\x1b[{parked_row}A")
            parts.append(CARRIAGE_RETURN)
            parked_row = 0
            parts.append(self._forget_painted_image())
            if image is not None:
                image_id = self._kitty_images[image.sha256][0]
                self._kitty_pending_placements[(image_id, image.placement_id)] = image.sha256
                if image.row:
                    parts.append(f"\x1b[{image.row}B")
                if image.column:
                    parts.append(f"\x1b[{image.column + 1}G")
                parts.append(place_image(image_id, image.placement_id, image.columns).decode("ascii"))
                if image.row:
                    parts.append(f"\x1b[{image.row}A")
                parts.append(CARRIAGE_RETURN)
            self._painted_image = image
        if parked_row:
            parts.append(f"\x1b[{parked_row}A")
        parts.append(CARRIAGE_RETURN)
        if cursor is not None:
            row, column = cursor
            if row:
                parts.append(f"\x1b[{row}B")
            parts.append(f"\x1b[{column + 1}G")
        parts.append(self._paint_end(show_cursor=cursor is not None))
        self._emit("".join(parts))
        self._painted = rendered
        self._painted_cursor = cursor
        self._painted_width = self.width

    def enter_alt_screen(self) -> None:
        """Borrow the alternate screen only when its original state is known off."""
        if self._closed or not self._is_tty or self._alt or os.name == "nt" or self._private_modes.get(1049) is not False:
            return
        self._emit(
            self._forget_painted_image()
            + self._cursor_to_live_origin()
            + ALTERNATE_SCREEN_ON
            + (HIDE_CURSOR if self._private_modes.get(25) is not None else "")
        )
        self._alt = True
        self._painted = []
        self._painted_cursor = None

    def leave_alt_screen(self) -> None:
        """Return to the entry buffer and force a full repaint of the live region."""
        if not self._alt:
            return
        self._alt = False
        self._painted = []
        # Entering the alternate buffer saved the main-buffer caret at its live origin.
        self._painted_cursor = (0, 0)
        self._resize = True
        cursor = self._private_modes.get(25)
        restore_cursor = SHOW_CURSOR if cursor is True else HIDE_CURSOR if cursor is False else ""
        self._emit(self._forget_painted_image() + ALTERNATE_SCREEN_OFF + restore_cursor)

    def _forget_painted_image(self) -> str:
        image = self._painted_image
        self._painted_image = None
        if image is None:
            return ""
        cached = self._kitty_images.get(image.sha256)
        if cached is None:
            return ""
        image_id = cached[0]
        self._kitty_pending_placements[(image_id, image.placement_id)] = image.sha256
        return delete_placement(image_id, image.placement_id).decode("ascii")

    def _frame_row_width(self) -> int:
        if not self._is_tty or self._private_modes.get(7) is not None:
            return self.width
        return max(1, self.width - 1)

    def _paint_begin(self) -> str:
        return "".join(
            (
                HIDE_CURSOR if self._private_modes.get(25) is not None else "",
                SYNC_BEGIN if self._private_modes.get(2026) is False else "",
                AUTOWRAP_OFF if self._private_modes.get(7) is True else "",
            )
        )

    def _paint_end(self, *, show_cursor: bool = True) -> str:
        cursor_mode = self._private_modes.get(25)
        cursor = (SHOW_CURSOR if show_cursor else HIDE_CURSOR) if cursor_mode is not None else ""
        return "".join(
            (
                AUTOWRAP_ON if self._private_modes.get(7) is True else "",
                SYNC_END if self._private_modes.get(2026) is False else "",
                cursor,
            )
        )

    def _cursor_to_live_origin(self) -> str:
        """Return the cached native caret to the owned live-region origin."""
        if self._painted_cursor is None:
            return ""
        row = self._painted_cursor[0]
        return (f"\x1b[{row}A" if row else "") + CARRIAGE_RETURN

    def _newline(self) -> str:
        # Raw mode clears OPOST, so committed rows must carry their own CR.
        return "\r\n" if self._raw else "\n"

    def _geometry_stale(self) -> bool:
        stale = self._resize or self._painted_width not in (0, self.width)
        self._resize = False
        return stale

    def _reset_live(self) -> None:
        if self._is_tty:
            self._emit(self._paint_begin() + self._cursor_to_live_origin() + self._forget_painted_image() + ERASE_BELOW + self._paint_end())
        self._painted = []
        self._painted_cursor = None
        self._painted_width = self.width
