from __future__ import annotations

from collections.abc import Mapping

from voidcode.capabilities.history_renderer import (
    iter_event_blocks,
    render_event_block,
    render_history_page,
    utf8_cut,
)


class _TestEvent:
    def __init__(
        self,
        sequence: int,
        event_type: str,
        source: str = "core",
        payload: Mapping[str, object] | None = None,
        payload_truncated: bool = False,
    ) -> None:
        self.sequence = sequence
        self.event_type = event_type
        self.source = source
        self.payload = payload
        self.payload_truncated = payload_truncated


def test_render_event_block_user_message() -> None:
    event = _TestEvent(1, "core.user_message", "user", {"content": "Hello world"})
    block = render_event_block(event)
    assert block == "## user [1]\nHello world"


def test_render_event_block_internal_core_omitted() -> None:
    event = _TestEvent(2, "core.internal_marker", "core", {})
    assert render_event_block(event) is None


def test_render_event_block_plugin_rendered() -> None:
    event = _TestEvent(3, "plugin.todo.updated", "todo", {"count": 2})
    block = render_event_block(event)
    assert block is not None
    assert "## event plugin.todo.updated from todo [3]" in block
    assert '"count": 2' in block


def test_iter_event_blocks_skips_none() -> None:
    events = [
        _TestEvent(1, "core.user_message", "user", {"content": "First"}),
        _TestEvent(2, "core.internal_marker", "core", {}),
        _TestEvent(3, "core.user_message", "user", {"content": "Second"}),
    ]
    blocks = list(iter_event_blocks(events))
    assert len(blocks) == 2
    assert "First" in blocks[0]
    assert "Second" in blocks[1]


def test_utf8_cut() -> None:
    # 3-byte utf-8 characters: '你' (3 bytes: e4 bd a0), '好' (3 bytes: e5 a5 bd)
    raw = "你好".encode()
    assert len(raw) == 6
    cut = utf8_cut(raw, 4)
    # cut should snap to utf-8 boundary
    assert cut == 3
    assert raw[:cut].decode("utf-8")


def test_render_history_page_paging() -> None:
    events = [
        _TestEvent(1, "core.user_message", "user", {"content": "Hello"}),
        _TestEvent(2, "core.user_message", "user", {"content": "World"}),
    ]
    page = render_history_page(events, after_sequence=0, max_bytes=1000)
    assert not page.truncated
    assert "Hello" in page.text
    assert "World" in page.text
    assert page.next_after_sequence == 2
