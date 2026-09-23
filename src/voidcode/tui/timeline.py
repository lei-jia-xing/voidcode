from __future__ import annotations

from typing import Any

from rich.console import RenderableType
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Static


class TimelineView(VerticalScroll):
    """Interactive transcript made of independently expandable event blocks."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tool_blocks: dict[str, Collapsible] = {}
        self._tool_bodies: dict[str, Static] = {}
        self._live_entries: dict[str, Static] = {}
        self._entries: list[Widget] = []
        self.max_lines = 2000
        self.wrap = True
        self.expanded = False

    def write(self, renderable: RenderableType, *, classes: str = "timeline-entry") -> Static:
        entry = Static(renderable, classes=classes)
        self._entries.append(entry)
        self.mount(entry)
        self._prune_entries()
        self.scroll_end(animate=False)
        return entry

    def write_live(
        self,
        key: str,
        renderable: RenderableType,
        *,
        classes: str = "timeline-entry assistant-stream",
    ) -> Static:
        entry = self.write(renderable, classes=classes)
        self._live_entries[key] = entry
        return entry

    def update_live(self, key: str, renderable: RenderableType) -> bool:
        entry = self._live_entries.get(key)
        if entry is None:
            return False
        entry.update(renderable)
        self.scroll_end(animate=False)
        return True

    def finish_live(self, key: str) -> None:
        self._live_entries.pop(key, None)

    def write_block(
        self,
        title: str,
        content: RenderableType = "",
        *,
        key: str | None = None,
        collapsed: bool = True,
        classes: str = "timeline-block",
    ) -> Collapsible:
        body = Static(content, classes="timeline-block-content")
        block = Collapsible(body, title=title, collapsed=collapsed and not self.expanded, classes=classes)
        self._entries.append(block)
        self.mount(block)
        if key:
            self._tool_blocks[key] = block
            self._tool_bodies[key] = body
        self.scroll_end(animate=False)
        self._prune_entries()
        return block

    def update_block(
        self,
        key: str,
        *,
        title: str | None = None,
        content: RenderableType | None = None,
        classes: str | None = None,
    ) -> None:
        block = self._tool_blocks.get(key)
        if block is None:
            return
        if title is not None:
            block.title = title
        if content is not None:
            self._tool_bodies[key].update(content)
        if classes is not None:
            block.set_classes(classes)

    def has_block(self, key: str) -> bool:
        return key in self._tool_blocks

    def expand_block(self, key: str) -> bool:
        block = self._tool_blocks.get(key)
        if block is None:
            return False
        block.collapsed = False
        self.scroll_to_widget(block, animate=False)
        return True

    def collapse_block(self, key: str) -> bool:
        block = self._tool_blocks.get(key)
        if block is None:
            return False
        block.collapsed = True
        return True

    def toggle_all_blocks(self) -> bool:
        self.expanded = not self.expanded
        for block in self._tool_blocks.values():
            block.collapsed = not self.expanded
        return self.expanded

    def clear(self) -> None:
        self._tool_blocks.clear()
        self._tool_bodies.clear()
        self._live_entries.clear()
        self._entries.clear()
        for child in tuple(self.children):
            child.remove()

    def _prune_entries(self) -> None:
        while len(self._entries) > self.max_lines:
            oldest = self._entries.pop(0)
            for key, block in tuple(self._tool_blocks.items()):
                if block is oldest:
                    self._tool_blocks.pop(key, None)
                    self._tool_bodies.pop(key, None)
            for key, entry in tuple(self._live_entries.items()):
                if entry is oldest:
                    self._live_entries.pop(key, None)
            oldest.remove()
