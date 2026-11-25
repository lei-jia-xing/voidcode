"""Typed overlay outcomes and bounded draft-value snapshots for plugin views."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import Literal

#: One form draft value. Plugin prefill and load replies arrive as JSON, so a
#: draft is a bounded JSON value; ``validate_form_submission`` stays the only
#: place field types, omission and numeric coercion are enforced.
type FormValue = str | int | float | bool | None | tuple[FormValue, ...] | Mapping[str, FormValue]

#: One ask draft answer. Text and numeric answers stay raw strings until the
#: shared server validator coerces them; booleans and multi-select id lists
#: are already typed.
type AskValue = str | bool | list[str] | None


@dataclass(frozen=True, slots=True)
class OverlayCancelled:
    """The overlay closed itself without dispatching anything."""


@dataclass(frozen=True, slots=True)
class OverlayActionSelected:
    """One revision-bound command action selected in an overlay."""

    action: str
    arguments: Mapping[str, object] = dataclass_field(default_factory=dict)
    prepare_input: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ConfirmationDecided:
    """A plugin confirmation decision; the action label stays verbatim."""

    request_id: str
    decision: str


@dataclass(frozen=True, slots=True)
class ApprovalDecided:
    """A native tool-approval decision (allow/deny, optional reason/policy)."""

    request_id: str
    decision: Literal["allow", "deny"]
    grant_session_policy: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AskAnswered:
    """One typed ask answer payload for the durable request."""

    request_id: str
    values: Mapping[str, AskValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class AskCancelled:
    """The ask overlay asked to cancel its durable request."""

    request_id: str
