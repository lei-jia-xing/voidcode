"""Composer completion popup state and candidate acceptance."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .composer import Composer


class ComposerCompletion:
    """Popup candidates, highlighted index, and exact-token replacement."""

    @property
    def completion(self: Composer) -> tuple[str, ...]:
        """Open completion candidates (empty when no popup is showing)."""
        return self._completion

    @property
    def completion_index(self: Composer) -> int:
        """Highlighted candidate while the popup is open."""
        return self._completion_index

    @property
    def completion_range(self: Composer) -> tuple[int, int] | None:
        """Active token [start, end) range being completed."""
        return self._completion_range

    def set_completion(
        self: Composer,
        candidates: Sequence[str],
        index: int = 0,
        target_range: tuple[int, int] | None = None,
    ) -> None:
        """Open the popup over ``candidates`` (empty closes it)."""
        self._completion = tuple(candidates)
        self._completion_index = min(max(0, index), max(0, len(self._completion) - 1))
        self._completion_range = target_range if self._completion else None

    def accept_completion_with(self: Composer, replacement: str) -> None:
        """Replace the target token (or draft) with ``replacement``."""
        if self._completion_range is not None:
            start, end = self._completion_range
            del_start, del_end = self._expand_delete_for_collapsed(start, end) if start < end else (start, end)
            if del_start < del_end:
                self._text = self._text[:del_start] + self._text[del_end:]
                self._adjust_blocks_for_delete(del_start, del_end)
            if replacement:
                self._adjust_blocks_for_insert(del_start, len(replacement))
                self._text = self._text[:del_start] + replacement + self._text[del_start:]
            self._cursor = del_start + len(replacement)
        else:
            self._text = replacement
            self._cursor = len(self._text)
            self._paste_blocks = []
        self.set_completion(())
        self._edited()

    def _accept_completion(self: Composer) -> None:
        """Replace the active token (or draft) with the highlighted candidate."""
        if not self._completion:
            return
        self.accept_completion_with(self._completion[self._completion_index])
