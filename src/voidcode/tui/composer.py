"""Stable composer facade: one editor state machine assembled from cohesive mixins.

Editing primitives live in ``composer_editing``, submitted-prompt history in
``composer_history``, popup state in ``composer_completion``, key handlers in
``composer_keys``, frame rendering in ``composer_rendering``, and the pure
cell-safe text math in ``composer_text``. ``Composer`` stays the single public
object used by ``CoreTuiApp`` and tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from .composer_completion import ComposerCompletion
from .composer_editing import ComposerEditing
from .composer_history import ComposerHistory
from .composer_keys import ComposerKeys
from .composer_rendering import _FRAMES, ComposerRendering
from .composer_text import _PasteBlock
from .composer_types import SHELL_ESCAPE_PREFIX, ComposerAction, ComposerOutcome
from .keys import Key
from .styles import Styles

__all__ = ["SHELL_ESCAPE_PREFIX", "Composer", "ComposerAction", "ComposerOutcome"]

#: The native terminal cursor is the sole insertion-point indicator.

_NEWLINE_KEYS = frozenset({"shift+enter", "ctrl+j", "ctrl+enter", "alt+enter"})
_WORD_LEFT_KEYS = frozenset({"alt+left", "alt+b", "ctrl+left"})
_WORD_RIGHT_KEYS = frozenset({"alt+right", "alt+f", "ctrl+right"})
_DELETE_WORD_BACK_KEYS = frozenset({"ctrl+w", "alt+backspace", "ctrl+backspace"})
_DELETE_WORD_FORWARD_KEYS = frozenset({"alt+delete", "alt+d"})
#: Keys whose ``text`` is not plain inserted text.
_NON_TEXT_KEYS = frozenset({"unknown", "paste"})
#: Paste-block chords (reserved in terminal_app; pure view-state toggles, no I/O).
_EXPAND_KEYS = frozenset({"alt+e", "alt+shift+e"})
_COPY_KEYS = frozenset({"alt+c", "alt+shift+c"})
_DELETE_BLOCK_KEYS = frozenset({"alt+x", "alt+shift+x"})


class Composer(ComposerCompletion, ComposerEditing, ComposerHistory, ComposerKeys, ComposerRendering):
    """Pure editor state machine and row renderer."""

    def __init__(
        self,
        *,
        styles: Styles | None = None,
        width: int,
        placeholder: str = "",
        max_height: int = 10,
        style: str = "box",
        gutter: str = "prompt",
        padding: str = "normal",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._styles = styles if styles is not None else Styles()
        self._width = max(1, width)
        self._placeholder = placeholder
        self._max_height = max(1, max_height)
        self._style = style
        self._gutter = gutter
        self._padding = padding
        self._clock = time.monotonic if clock is None else clock
        self._text = ""
        self._cursor = 0
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self._draft_blocks: list[_PasteBlock] = []
        self._paste_blocks: list[_PasteBlock] = []
        self._last_interrupt = float("-inf")
        self._scroll = 0
        self._cursor_position: tuple[int, int] | None = None
        self._completion: tuple[str, ...] = ()
        self._completion_index = 0
        self._completion_range: tuple[int, int] | None = None
        self.completion_descriptions: dict[str, str] = {}
        self.completion_groups: set[str] = set()

    @property
    def is_bash_mode(self) -> bool:
        """Whether the draft is a native `!` shell escape (upstream bash mode)."""
        return self._text.startswith(SHELL_ESCAPE_PREFIX)

    # -- accessors ---------------------------------------------------------

    @property
    def value(self) -> str:
        """Current draft text."""
        return self._text

    @property
    def cursor(self) -> int:
        """Caret offset in the draft (grapheme-snapped by editing keys)."""
        return self._cursor

    @property
    def cursor_position(self) -> tuple[int, int] | None:
        """Native cursor location in the most recently rendered composer frame."""
        return self._cursor_position

    @property
    def paste_blocks(self) -> tuple[tuple[int, int, bool], ...]:
        """Folded paste spans as ``(start, end, collapsed)`` in true-text offsets."""
        return tuple((block.start, block.end, block.collapsed) for block in self._paste_blocks)

    @property
    def display_text(self) -> str:
        """Draft with collapsed blocks replaced by their single-row badges."""
        text, _ = self._display_text_and_cursor()
        return text

    def set_max_height(self, height: int) -> None:
        self._max_height = max(1, height)

    def set_width(self, width: int) -> None:
        self._width = max(1, width)

    def status_width(self) -> int:
        """Width reserved for the single app-owned statusline row."""
        frame = _FRAMES[self._style]
        inset = 2 if frame.sides or frame.status_dock == "band" else 1 if frame.sidebar else 0
        return max(1, self._width - inset)

    def input_height(self, terminal_height: int, *, status_visible: bool = True) -> int:
        """Input capacity after fixed frame, padding, and status-dock rows."""
        frame = _FRAMES[self._style]
        fixed = int(frame.top_rule) + int(frame.bottom_rule)
        if self._padding == "normal":
            fixed += 2
        if status_visible:
            fixed += 1
        return max(1, min(10, terminal_height - fixed))

    def clear(self) -> None:
        """Drop the draft and any history browsing state."""
        self._text = ""
        self._cursor = 0
        self._history_index = None
        self._draft = ""
        self._draft_blocks = []
        self._paste_blocks = []
        self._scroll = 0
        self.set_completion(())

    def set_text(self, text: str) -> None:
        """Replace the draft with ``text`` (history insertion); the caret lands at the end."""
        self._text = text
        self._cursor = len(text)
        self._history_index = None
        self._paste_blocks = []
        self._scroll = 0
        self.set_completion(())

    @staticmethod
    def owns_key(name: str) -> bool:
        """Whether the composer itself consumes this key name (never a catalog chord)."""
        return name in _KEYMAP

    # -- key handling ------------------------------------------------------

    def handle_key(self, key: Key) -> ComposerOutcome:
        """Fold one key into the composer; report what the app should do."""
        name = key.name
        if name == "escape" and self._text == "!":
            # Upstream bash mode: Esc exits the empty `!` mode instead of
            # cancelling a turn.
            self.clear()
            return ComposerOutcome()
        handler = _KEYMAP.get(name)
        if handler is not None:
            return handler(self, key)
        if key.text and name not in _NON_TEXT_KEYS:
            self._insert(key.text)
        return ComposerOutcome()

    def _submit(self) -> ComposerOutcome:
        text = self._text
        if not text.strip():
            return ComposerOutcome()
        self._push_history(text)
        self.clear()
        return ComposerOutcome(ComposerAction.SUBMIT, text)


#: Key name → handler: the whole composer dispatch as data. Names are disjoint
#: across singles and key sets, so every key resolves to exactly one handler;
#: the escape-with-"!" pre-check and the text fallback stay in handle_key.
_KEYMAP: Final[dict[str, Callable[[Composer, Key], ComposerOutcome]]] = {
    "escape": Composer._key_escape,
    "ctrl+c": Composer._key_ctrl_c,
    "tab": Composer._key_tab,
    "enter": Composer._key_enter,
    "paste": Composer._key_paste,
    "left": Composer._key_left,
    "right": Composer._key_right,
    "up": Composer._key_up,
    "down": Composer._key_down,
    "home": Composer._key_home,
    "ctrl+a": Composer._key_home,
    "end": Composer._key_end,
    "ctrl+e": Composer._key_end,
    "backspace": Composer._key_backspace,
    "delete": Composer._key_delete,
    "ctrl+d": Composer._key_delete,
    "ctrl+u": Composer._key_ctrl_u,
    "ctrl+k": Composer._key_ctrl_k,
    **{name: Composer._key_newline for name in _NEWLINE_KEYS},
    **{name: Composer._key_expand for name in _EXPAND_KEYS},
    **{name: Composer._key_copy for name in _COPY_KEYS},
    **{name: Composer._key_delete_block for name in _DELETE_BLOCK_KEYS},
    **{name: Composer._key_word_left for name in _WORD_LEFT_KEYS},
    **{name: Composer._key_word_right for name in _WORD_RIGHT_KEYS},
    **{name: Composer._key_delete_word_back for name in _DELETE_WORD_BACK_KEYS},
    **{name: Composer._key_delete_word_forward for name in _DELETE_WORD_FORWARD_KEYS},
}
