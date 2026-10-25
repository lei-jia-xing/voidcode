"""History event protocol, page container, and bounds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

#: One live history page stays well below the core's 50 KiB model projection, leaving room
#: for the result body and markers.
PAGE_BYTES: Final[int] = 40 * 1024


class HistoryEvent(Protocol):
    """The attributes a stored event and a service event view both carry."""

    @property
    def sequence(self) -> int: ...

    @property
    def event_type(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One bounded page plus the cursor that reaches the next byte.

    ``next_after_sequence`` is the last event whose block was fully included (or the incoming
    cursor value when the first block is split). ``next_offset`` is the UTF-8 byte offset into
    the next block; zero means the next event after ``next_after_sequence`` starts at its head.
    ``truncated`` means more content remains.
    """

    text: str
    next_after_sequence: int
    next_offset: int
    truncated: bool
