"""ANSI control sequences and session-scoped terminal protocol constants."""

from __future__ import annotations

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET_STYLE = "\x1b[0m"
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
AUTOWRAP_OFF = "\x1b[?7l"
AUTOWRAP_ON = "\x1b[?7h"
ERASE_TO_END_OF_LINE = "\x1b[2K"
ERASE_BELOW = "\x1b[J"
CARRIAGE_RETURN = "\r"

#: Full-screen overlays may borrow it; transcript and inline live content stay
#: in the main buffer, preserving native scrollback.
ALTERNATE_SCREEN_ON = "\x1b[?1049h"
ALTERNATE_SCREEN_OFF = "\x1b[?1049l"

# Session-scoped terminal protocols. Terminal consumes private-mode query
# replies before user input; the keyboard enhancement probe is fire-and-forget.
# Its replies decode as unknown keys, which the composer does not insert.
BRACKETED_PASTE_ON = "\x1b[?2004h"
BRACKETED_PASTE_OFF = "\x1b[?2004l"
KEYBOARD_PROBE = "\x1b[?u\x1b[c"
KEYBOARD_ENABLE = "\x1b[>5u"
KEYBOARD_POP = "\x1b[<u"
