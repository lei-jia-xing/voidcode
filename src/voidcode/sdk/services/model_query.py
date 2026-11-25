"""Governed model-query results, structured parsing, and typed failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..json_values import json_wire_object, own_json_object

if TYPE_CHECKING:
    from ..provider import ProviderDoneReason


@dataclass(frozen=True, slots=True)
class StructuredQueryParseError:
    """Why one structured model reply produced no valid data.

    ``invalid_json`` covers empty, unparseable, and non-object top-level JSON
    (a refusal is unparseable text, not data). ``schema_violation`` covers
    well-formed JSON the result schema rejects. ``path`` is the failing JSON
    path of a schema violation, or ``None`` when nothing parsed.
    """

    kind: Literal["invalid_json", "schema_violation"]
    message: str
    path: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("invalid_json", "schema_violation"):
            raise ValueError(f"unknown structured query parse error kind {self.kind!r}")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("structured query parse error requires a non-empty message")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True, slots=True)
class StructuredModelResult:
    """One tool-free provider turn parsed against a caller-supplied result schema.

    ``raw_text`` is the verbatim model reply, always kept. Exactly one of
    ``data`` (owned, schema-validated JSON object) and ``parse_error`` is set:
    an invalid reply is a callable failure, never silent success and never
    data. ``done_reason`` is the provider's own terminal marker, passed through
    unchanged. Cancellation, budget exhaustion, and provider errors propagate
    as exceptions — exactly as the bare ``model_query`` — and never surface here.
    """

    raw_text: str
    data: Mapping[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None
    done_reason: ProviderDoneReason = "completed"

    def __post_init__(self) -> None:
        if self.data is not None:
            object.__setattr__(self, "data", own_json_object(self.data))
        if (self.data is None) == (self.parse_error is None):
            raise ValueError("structured model result requires exactly one of data or parse_error")

    def as_payload(self) -> dict[str, object]:
        return {
            "raw_text": self.raw_text,
            "data": None if self.data is None else json_wire_object(self.data),
            "parse_error": None if self.parse_error is None else self.parse_error.as_payload(),
            "done_reason": self.done_reason,
        }


@dataclass(frozen=True, slots=True)
class StructuredChildResult:
    """One delegated child run read against the parent's declared result schema.

    ``raw_text`` is the child's full terminal text, always kept. Exactly one of
    ``data`` (owned, schema-validated JSON object) and ``parse_error`` is set:
    an invalid delivery is a callable failure, never silent success and never
    data. ``output_uri`` names the immutable full-output artifact when the run
    spilled one; ``None`` means the run completed inline (use ``raw_text``),
    never \"no output\". Non-completed runs have no structured reading at all:
    the caller reports status, never this shape.
    """

    child_session_id: str
    run_id: str | None
    raw_text: str
    data: Mapping[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None
    output_uri: str | None = None
    output_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.data is not None:
            object.__setattr__(self, "data", own_json_object(self.data))
        if (self.data is None) == (self.parse_error is None):
            raise ValueError("structured child result requires exactly one of data or parse_error")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": self.child_session_id,
            "run_id": self.run_id,
            "raw_text": self.raw_text,
            "data": None if self.data is None else json_wire_object(self.data),
            "parse_error": None if self.parse_error is None else self.parse_error.as_payload(),
        }
        if self.output_uri is not None:
            payload["output_uri"] = self.output_uri
        if self.output_bytes is not None:
            payload["output_bytes"] = self.output_bytes
        return payload


class ContextContributionFailed(RuntimeError):
    """A registered plugin context provider failed while assembling context."""


class ModelQueryError(Exception):
    """Base type for governed model-query failures; classification never uses message text."""


class ModelQueryCancelled(RuntimeError, ModelQueryError):
    """A governed model query was cancelled before it produced a usable answer."""


class ModelQueryToolCalls(RuntimeError, ModelQueryError):
    """A governed model query returned native tool calls although none were offered."""


class ModelQueryBudgetExhausted(RuntimeError, ModelQueryError):
    """An active session/plugin budget refused a governed model query before dispatch."""


class ModelQueryContextFailed(ContextContributionFailed, ModelQueryError):
    """A context provider failed while a governed model query assembled its context."""


class ModelQueryUnavailable(RuntimeError, ModelQueryError):
    """This runtime has no governed model-query callback."""


class ModelQueryModelUnavailable(ValueError, ModelQueryError):
    """No configured model, or a model name that is not an admitted provider/model."""


class ModelQueryPromptTooLong(ValueError, ModelQueryError):
    """The model-query prompt exceeds the core's UTF-8 prompt ceiling."""
