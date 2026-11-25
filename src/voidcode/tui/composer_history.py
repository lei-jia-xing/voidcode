"""Composer submitted-prompt history and draft restoration while browsing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .composer_text import _PasteBlock
from .composer_types import HISTORY_DEPTH

if TYPE_CHECKING:
    from .composer import Composer


class ComposerHistory:
    """Bounded history, draft snapshot, and paste-block restoration."""

    @property
    def history(self: Composer) -> tuple[str, ...]:
        """The bounded submitted-prompt history, oldest first."""
        return tuple(self._history)

    def load_history(self: Composer, items: Sequence[str]) -> None:
        """Seed or restore submitted-prompt history from durable session events."""
        self._history.clear()
        self._history_index = None
        for text in items:
            self._push_history(text)

    def _push_history(self: Composer, text: str) -> None:
        if not text.strip():
            return
        if self._history and self._history[-1] == text:
            return
        self._history.append(text)
        if len(self._history) > HISTORY_DEPTH:
            del self._history[0]

    def _history_up(self: Composer) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._draft = self._text
            self._draft_blocks = [_PasteBlock(block.start, block.end, block.collapsed) for block in self._paste_blocks]
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self._text = self._history[self._history_index]
        self._cursor = 0
        self._paste_blocks = []

    def _history_down(self: Composer) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._text = self._history[self._history_index]
            self._paste_blocks = []
        else:
            self._history_index = None
            self._text = self._draft
            self._paste_blocks = [_PasteBlock(block.start, block.end, block.collapsed) for block in self._draft_blocks]
        self._cursor = len(self._text)
