from __future__ import annotations

from textual.message import Message

from ..runtime.contracts import RuntimeStreamChunk


class StreamChunkReceived(Message):
    def __init__(self, chunk: RuntimeStreamChunk) -> None:
        super().__init__()
        self.chunk = chunk


class StreamCompleted(Message):
    def __init__(self, final_status: str) -> None:
        super().__init__()
        self.final_status = final_status


class StreamFailed(Message):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
