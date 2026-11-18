"""Suspend-by-unwind park/resume discipline shared by parked interaction contracts.

Both contracts ride one discipline: **suspend-by-unwind, park-by-commit,
resume-by-explicit-entry, adopt-without-re-invoke**. A tool callback suspends
by calling a ``PluginServices`` method that stages a wait-intent on the lease
and then raising a dedicated ``SuspendPark`` carrying only JSON-ownable
intent. Per-contract intent/terminal types live in ``foreground.py`` and
``ask.py`` and reuse this base.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .json_values import own_json_object


@dataclass(frozen=True, slots=True)
class ParkIdentity:
    """Durable identity of one committed park: Core-assigned park id plus its binding."""

    park_id: str
    session_id: str
    revision_id: str
    owner: str

    def __post_init__(self) -> None:
        for name in ("park_id", "session_id", "revision_id", "owner"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"park identity requires a non-empty {name}")

    def as_payload(self) -> dict[str, object]:
        return {
            "park_id": self.park_id,
            "session_id": self.session_id,
            "revision_id": self.revision_id,
            "owner": self.owner,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ParkIdentity:
        if not isinstance(payload, Mapping):
            raise ValueError("park identity payload must be an object")
        allowed = {"park_id", "session_id", "revision_id", "owner"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("park identity contains unknown field(s): " + ", ".join(unknown))
        missing = sorted(allowed - set(payload))
        if missing:
            raise ValueError("park identity is missing field(s): " + ", ".join(missing))
        values: dict[str, str] = {}
        for name in ("park_id", "session_id", "revision_id", "owner"):
            value = payload[name]
            if not isinstance(value, str) or not value:
                raise ValueError(f"park identity requires a non-empty {name}")
            values[name] = value
        return ParkIdentity(
            park_id=values["park_id"],
            session_id=values["session_id"],
            revision_id=values["revision_id"],
            owner=values["owner"],
        )


class SuspendPark(BaseException):
    """Unwind the callback to park it. Carries intent only; caught only at PluginRuntime.invoke."""

    def __init__(self, park_kind: Literal["composition", "ask"], intent: Mapping[str, object]) -> None:
        if park_kind not in ("composition", "ask"):
            raise ValueError(f"unknown park kind: {park_kind!r}")
        if not isinstance(intent, Mapping):
            raise ValueError("suspend intent must be a JSON object")
        self._park_kind: Literal["composition", "ask"] = park_kind
        self._intent = own_json_object(intent)
        super().__init__(park_kind)

    @property
    def park_kind(self) -> Literal["composition", "ask"]:
        return self._park_kind

    @property
    def intent(self) -> Mapping[str, object]:
        """The owned JSON intent staged on the lease (ids + validated data, never a frame)."""
        return self._intent


class ParkError(ValueError):
    """A park is unknown, not pending, or already terminal."""


class ParkRevisionError(ValueError):
    """An expected revision did not match the park's active revision."""


__all__ = [
    "ParkError",
    "ParkIdentity",
    "ParkRevisionError",
    "SuspendPark",
]
