from __future__ import annotations

from collections.abc import Mapping
from dataclasses import is_dataclass

from voidcode.sdk.history import PAGE_BYTES, HistoryEvent, HistoryPage


class _FakeEvent:
    def __init__(self, sequence: int, event_type: str, source: str, payload: Mapping[str, object] | None) -> None:
        self.sequence = sequence
        self.event_type = event_type
        self.source = source
        self.payload = payload


def test_history_contracts() -> None:
    assert PAGE_BYTES == 40 * 1024

    event: HistoryEvent = _FakeEvent(1, "core.user_message", "user", {"content": "hello"})
    assert event.sequence == 1
    assert event.event_type == "core.user_message"
    assert event.source == "user"
    assert event.payload == {"content": "hello"}

    assert is_dataclass(HistoryPage)
    page = HistoryPage("text", next_after_sequence=1, next_offset=0, truncated=False)
    assert page.text == "text"
    assert page.next_after_sequence == 1
    assert page.next_offset == 0
    assert page.truncated is False
