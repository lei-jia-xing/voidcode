from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .command.models import CommandDefinition
from .runtime.events import EventEnvelope
from .runtime.session import SessionState, StoredSessionSummary

EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_CONFIG_ERROR = 10
EXIT_PROVIDER_ERROR = 11
EXIT_RUNTIME_ERROR = 12
EXIT_APPROVAL_DENIED = 13
EXIT_CANCELLED = 14
EXIT_INVALID_COMMAND = 15
EXIT_INVALID_RESOURCE = 16


@dataclass(frozen=True, slots=True)
class RuntimeStreamResult:
    output: str | None
    session: SessionState
    events: tuple[EventEnvelope, ...]


def print_json(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True))


def format_event(event_type: str, source: str, data: dict[str, object]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in sorted(data.items()))
    if suffix:
        return f"EVENT {event_type} source={source} {suffix}"
    return f"EVENT {event_type} source={source}"


def serialize_event(event: EventEnvelope) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "source": event.source,
        "payload": event.payload,
    }


def serialize_session_state(session: SessionState) -> dict[str, object]:
    session_payload: dict[str, object] = {"id": session.session.id}
    parent_id = getattr(session.session, "parent_id", None)
    if parent_id is not None:
        session_payload["parent_id"] = parent_id
    return {
        "session": session_payload,
        "status": session.status,
        "turn": session.turn,
        "metadata": session.metadata,
    }


def serialize_stored_session_summary(session: StoredSessionSummary) -> dict[str, object]:
    return {
        "id": session.session.id,
        "parent_id": getattr(session.session, "parent_id", None),
        "status": session.status,
        "turn": session.turn,
        "updated_at": session.updated_at,
        "prompt": session.prompt,
    }


def serialize_command_definition(command: CommandDefinition) -> dict[str, object]:
    return {
        "name": command.name,
        "description": command.description,
        "source": command.source,
        "enabled": command.enabled,
        "hidden": command.hidden,
        "agent": command.agent,
        "model": command.model,
        "subtask": command.subtask,
        "path": _serialize_path(command.path),
        "template": command.template,
    }


def serialize_command_summary(command: CommandDefinition) -> dict[str, object]:
    return {
        key: value
        for key, value in serialize_command_definition(command).items()
        if key != "template"
    }


def _serialize_path(path: Path | None) -> str | None:
    return None if path is None else str(path)
