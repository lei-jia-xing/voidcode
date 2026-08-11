"""Runtime-owned steering and follow-up message queues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

type QueuedMessageKind = Literal["steering", "follow_up"]


@dataclass(frozen=True, slots=True)
class QueuedRuntimeMessage:
    id: str
    kind: QueuedMessageKind
    content: str

    def metadata_payload(self) -> dict[str, object]:
        return {"id": self.id, "kind": self.kind, "content": self.content}


def enqueue_runtime_message(
    metadata: dict[str, object],
    *,
    content: str,
    kind: QueuedMessageKind,
) -> dict[str, object]:
    normalized = content.strip()
    if not normalized:
        raise ValueError("queued runtime message must not be empty")
    state = dict(metadata)
    raw = state.get("pending_messages")
    messages = list(raw) if isinstance(raw, list) else []
    messages.append(QueuedRuntimeMessage(id=uuid4().hex, kind=kind, content=normalized).metadata_payload())
    state["pending_messages"] = messages[-50:]
    return state


def drain_runtime_messages(
    metadata: dict[str, object],
    *,
    kind: QueuedMessageKind,
) -> tuple[dict[str, object], tuple[QueuedRuntimeMessage, ...]]:
    raw = metadata.get("pending_messages")
    if not isinstance(raw, list):
        return dict(metadata), ()
    drained: list[QueuedRuntimeMessage] = []
    remaining: list[object] = []
    for item in raw:
        payload = cast(dict[str, object], item) if isinstance(item, dict) else None
        if payload is None or payload.get("kind") != kind:
            remaining.append(item)
            continue
        message_id = payload.get("id")
        content = payload.get("content")
        if isinstance(message_id, str) and isinstance(content, str) and content.strip():
            drained.append(QueuedRuntimeMessage(id=message_id, kind=kind, content=content))
    result = dict(metadata)
    if remaining:
        result["pending_messages"] = remaining
    else:
        result.pop("pending_messages", None)
    return result, tuple(drained)


__all__ = ["QueuedRuntimeMessage", "QueuedMessageKind", "drain_runtime_messages", "enqueue_runtime_message"]
