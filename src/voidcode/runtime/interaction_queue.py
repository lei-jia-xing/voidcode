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
    dedupe_key: str | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"id": self.id, "kind": self.kind, "content": self.content}
        if self.dedupe_key is not None:
            payload["dedupe_key"] = self.dedupe_key
        return payload


_DELIVERY_CURSOR_METADATA_KEY = "runtime_interaction_delivery_cursor"


def _is_durable_delivery_key(value: str | None) -> bool:
    return value is not None and value.startswith(("background-task-completion:", "background-task-progress:"))


_DELIVERY_CURSOR_LIMIT = 128


def _delivery_cursor(metadata: dict[str, object]) -> tuple[str, ...]:
    raw = metadata.get(_DELIVERY_CURSOR_METADATA_KEY)
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))[-_DELIVERY_CURSOR_LIMIT:]


def enqueue_runtime_message(
    metadata: dict[str, object],
    *,
    content: str,
    kind: QueuedMessageKind,
    dedupe_key: str | None = None,
) -> dict[str, object]:
    normalized = content.strip()
    if not normalized:
        raise ValueError("queued runtime message must not be empty")
    state = dict(metadata)
    raw = state.get("pending_messages")
    messages = list(raw) if isinstance(raw, list) else []
    cursor = _delivery_cursor(state)
    if (
        dedupe_key is not None
        and _is_durable_delivery_key(dedupe_key)
        and (dedupe_key in cursor or any(isinstance(item, dict) and item.get("dedupe_key") == dedupe_key for item in messages))
    ):
        return state
    messages.append(
        QueuedRuntimeMessage(
            id=uuid4().hex,
            kind=kind,
            content=normalized,
            dedupe_key=dedupe_key,
        ).metadata_payload()
    )
    state["pending_messages"] = messages[-50:]
    return state


def drain_runtime_messages(
    metadata: dict[str, object],
    *,
    kind: QueuedMessageKind,
    remember_dedupe: bool = False,
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
        dedupe_key = payload.get("dedupe_key")
        if isinstance(message_id, str) and isinstance(content, str) and content.strip():
            drained.append(
                QueuedRuntimeMessage(
                    id=message_id,
                    kind=kind,
                    content=content,
                    dedupe_key=(dedupe_key if isinstance(dedupe_key, str) else None),
                )
            )
    result = dict(metadata)
    if remaining:
        result["pending_messages"] = remaining
    else:
        result.pop("pending_messages", None)
    if remember_dedupe:
        cursor = list(_delivery_cursor(result))
        for message in drained:
            dedupe_key = message.dedupe_key
            if isinstance(dedupe_key, str) and _is_durable_delivery_key(dedupe_key) and dedupe_key not in cursor:
                cursor.append(dedupe_key)
        if cursor:
            result[_DELIVERY_CURSOR_METADATA_KEY] = cursor[-_DELIVERY_CURSOR_LIMIT:]
    return result, tuple(drained)


__all__ = ["QueuedRuntimeMessage", "QueuedMessageKind", "drain_runtime_messages", "enqueue_runtime_message"]
