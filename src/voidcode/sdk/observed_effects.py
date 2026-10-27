"""Observed true effects of confined execution: the sandbox's deliverable.

Confinement is the means; these bounded reports are what callers map into
``ToolSuccess``/``ToolFailure.changes``, ``coverage``, and ``diagnostics``.
Contracts only: no I/O, no subprocess, no backend knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from .json_values import json_wire_object

#: Per-execution bound on reported changes; excess sets ``truncated=True``.
MAX_OBSERVED_CHANGES: Final[int] = 256
#: Bound on reported denials per execution.
MAX_OBSERVED_DENIALS: Final[int] = 64
#: Bytes the helper hashes eagerly per file during snapshot-diff.
SNAPSHOT_HASH_BYTES: Final[int] = 1024 * 1024
#: Single READ page bound in bytes.
READ_PAGE_BYTES: Final[int] = 1024 * 1024
#: Single WRITE bound in bytes (matches the file tools' wire bound).
WRITE_MAX_BYTES: Final[int] = 64 * 1024
#: LIST/WALK entry bound.
LIST_MAX_ENTRIES: Final[int] = 10_000
#: Symlink chain elements the helper canonicalizes before refusing.
MAX_SYMLINK_FOLLOW: Final[int] = 8

ObservedOperation = Literal["create", "update", "delete", "move"]


@dataclass(frozen=True, slots=True)
class ObservedChange:
    """One filesystem change the helper observed, mapped 1:1 to ``ResourceChange``."""

    path: str
    operation: ObservedOperation = "update"
    old_path: str | None = None
    content_hash: str | None = None
    #: The write landed in sandbox scratch (tmpfs), not on host paths: surface in
    #: output text/body only, never as a ``ResourceChange``.
    scratch: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("observed change requires a non-empty path")
        if self.operation not in {"create", "update", "delete", "move"}:
            raise ValueError("invalid observed change operation")
        if self.old_path is not None and (not isinstance(self.old_path, str) or not self.old_path):
            raise ValueError("old_path must be a non-empty string or None")
        if self.operation == "move" and not self.old_path:
            raise ValueError("observed move requires old_path")
        if self.content_hash is not None and (not isinstance(self.content_hash, str) or not self.content_hash):
            raise ValueError("content_hash must be a non-empty string or None")
        if type(self.scratch) is not bool:
            raise ValueError("scratch must be a boolean")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"path": self.path, "operation": self.operation}
        if self.old_path is not None:
            payload["old_path"] = self.old_path
        if self.content_hash is not None:
            payload["content_hash"] = self.content_hash
        if self.scratch:
            payload["scratch"] = True
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> ObservedChange:
        if not isinstance(payload, dict):
            raise ValueError("observed change must be an object")
        unknown = sorted(set(payload) - {"path", "operation", "old_path", "content_hash", "scratch"})
        if unknown:
            raise ValueError("observed change contains unknown field(s): " + ", ".join(unknown))
        return cls(
            path=_required_str(payload.get("path"), "path"),
            operation=payload.get("operation", "update"),  # type: ignore[arg-type]
            old_path=_optional_str(payload.get("old_path"), "old_path"),
            content_hash=_optional_str(payload.get("content_hash"), "content_hash"),
            scratch=bool(payload.get("scratch", False)),
        )


@dataclass(frozen=True, slots=True)
class ObservedDenial:
    """A policy refusal the helper computed itself: exact operation, target, and reason."""

    operation: str
    path: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not self.operation:
            raise ValueError("observed denial requires a non-empty operation")
        if self.path is not None and (not isinstance(self.path, str) or not self.path):
            raise ValueError("denial path must be a non-empty string or None")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("observed denial requires a non-empty reason")

    def as_payload(self) -> dict[str, object]:
        """THE shared denial serialization; every tool uses this shape."""
        payload: dict[str, object] = {"operation": self.operation, "reason": self.reason}
        if self.path is not None:
            payload["path"] = self.path
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> ObservedDenial:
        if not isinstance(payload, dict):
            raise ValueError("observed denial must be an object")
        unknown = sorted(set(payload) - {"operation", "path", "reason"})
        if unknown:
            raise ValueError("observed denial contains unknown field(s): " + ", ".join(unknown))
        return cls(
            operation=_required_str(payload.get("operation"), "operation"),
            path=_optional_str(payload.get("path"), "path"),
            reason=_required_str(payload.get("reason"), "reason"),
        )


@dataclass(frozen=True, slots=True)
class NetworkObservation:
    """Enforced network posture for one execution; per-attempt tracing is never invented."""

    allowed: bool
    detail: str
    attempts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("network observation allowed must be a boolean")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("network observation requires a non-empty detail")
        object.__setattr__(self, "attempts", tuple(self.attempts))

    def as_payload(self) -> dict[str, object]:
        return {"allowed": self.allowed, "detail": self.detail, "attempts": list(self.attempts)}

    @classmethod
    def from_payload(cls, payload: object) -> NetworkObservation:
        if not isinstance(payload, dict):
            raise ValueError("network observation must be an object")
        unknown = sorted(set(payload) - {"allowed", "detail", "attempts"})
        if unknown:
            raise ValueError("network observation contains unknown field(s): " + ", ".join(unknown))
        allowed = payload.get("allowed")
        if type(allowed) is not bool:
            raise ValueError("network observation allowed must be a boolean")
        attempts = payload.get("attempts", ())
        if not isinstance(attempts, (list, tuple)) or not all(isinstance(a, str) for a in attempts):
            raise ValueError("network observation attempts must be a list of strings")
        return cls(allowed=allowed, detail=_required_str(payload.get("detail"), "detail"), attempts=tuple(attempts))


@dataclass(frozen=True, slots=True)
class ObservedEffects:
    """Bounded observed-effects report for one sandboxed exec or fs-op."""

    changes: tuple[ObservedChange, ...] = ()
    denials: tuple[ObservedDenial, ...] = ()
    network: NetworkObservation | None = None
    backend: str = "unconfined"
    helper: str = ""
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "denials", tuple(self.denials))
        if len(self.changes) > MAX_OBSERVED_CHANGES:
            raise ValueError(f"observed changes exceed the {MAX_OBSERVED_CHANGES} bound")
        if len(self.denials) > MAX_OBSERVED_DENIALS:
            raise ValueError(f"observed denials exceed the {MAX_OBSERVED_DENIALS} bound")
        if not isinstance(self.backend, str) or not self.backend:
            raise ValueError("observed effects require a non-empty backend identity")
        if not isinstance(self.helper, str):
            raise ValueError("observed effects helper must be a string")
        if type(self.truncated) is not bool:
            raise ValueError("observed effects truncated must be a boolean")

    def as_payload(self) -> dict[str, object]:
        return {
            "changes": [change.as_payload() for change in self.changes],
            "denials": [denial.as_payload() for denial in self.denials],
            "network": None if self.network is None else self.network.as_payload(),
            "backend": self.backend,
            "helper": self.helper,
            "truncated": self.truncated,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ObservedEffects:
        if not isinstance(payload, dict):
            raise ValueError("observed effects must be an object")
        unknown = sorted(set(payload) - {"changes", "denials", "network", "backend", "helper", "truncated"})
        if unknown:
            raise ValueError("observed effects contains unknown field(s): " + ", ".join(unknown))
        changes = payload.get("changes", ())
        denials = payload.get("denials", ())
        if not isinstance(changes, (list, tuple)):
            raise ValueError("observed changes must be a list")
        if not isinstance(denials, (list, tuple)):
            raise ValueError("observed denials must be a list")
        network = payload.get("network")
        truncated = payload.get("truncated", False)
        if type(truncated) is not bool:
            raise ValueError("observed effects truncated must be a boolean")
        owned = json_wire_object(payload)
        _ = owned
        return cls(
            changes=tuple(ObservedChange.from_payload(item) for item in changes),
            denials=tuple(ObservedDenial.from_payload(item) for item in denials),
            network=None if network is None else NetworkObservation.from_payload(network),
            backend=_required_str(payload.get("backend"), "backend"),
            helper=payload.get("helper", "") if isinstance(payload.get("helper", ""), str) else "",
            truncated=truncated,
        )


def _required_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"observed effects field {field!r} must be a non-empty string")
    return value


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"observed effects field {field!r} must be a non-empty string or null")
    return value
