"""One typed stack for every overlay that can own the live region.

The stack is the single source of truth behind ``CoreTuiApp.active_overlay``,
``CoreTuiApp.pending_approval`` and the confirmation/ask queues.  Transitions
are explicit methods returning typed outcomes; callers never synchronize
parallel fields by hand.

Priority is fixed and mirrors the old fields:

1. a native tool approval masks everything,
2. one active interactive overlay owns focus,
3. queued confirmations/asks wait in request order,
4. a live choices/markdown view suspended under its own confirmation is
   restored only after queued gates are gone and its revision still matches.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from . import events
from .views import InteractiveApproval, InteractiveAsk, InteractiveChoices, InteractiveConfirmation, InteractiveMarkdown

#: The two overlay kinds that can be suspended by their own confirmation.
type LiveOverlay = InteractiveChoices | InteractiveMarkdown

#: How one confirmation/ask request was handled.
type RequestDisposition = Literal["ignored", "duplicate", "shown", "suspended", "queued"]

#: What ``present`` put in front of the composer.
type PresentationKind = Literal["none", "confirmation", "ask", "live"]


@dataclass(frozen=True, slots=True)
class SuspendedLive:
    """A live choices/markdown overlay paused under its own confirmation."""

    overlay: LiveOverlay
    live_input: events.LiveInput


@dataclass(frozen=True, slots=True)
class OverlayPresentation:
    """Result of presenting the next queued gate or suspended live view."""

    kind: PresentationKind
    suspended: SuspendedLive | None = None

    def __post_init__(self) -> None:
        if self.kind == "live" and self.suspended is None:
            raise ValueError("a live presentation needs the restored suspended view")
        if self.kind != "live" and self.suspended is not None:
            raise ValueError("only a live presentation restores a suspended view")


NONE_PRESENTED = OverlayPresentation("none")


class OverlayStack:
    """Approval, active overlay, queued gates, and one suspended live view."""

    def __init__(self) -> None:
        self._approval: InteractiveApproval | None = None
        self._active: events.InteractiveOverlay | None = None
        self._confirmations: dict[str, InteractiveConfirmation] = {}
        self._asks: dict[str, InteractiveAsk] = {}
        self._suspended: SuspendedLive | None = None

    @property
    def approval(self) -> InteractiveApproval | None:
        return self._approval

    @property
    def active(self) -> events.InteractiveOverlay | None:
        return self._active

    @property
    def suspended(self) -> SuspendedLive | None:
        return self._suspended

    @property
    def queued_confirmation_ids(self) -> tuple[str, ...]:
        return tuple(self._confirmations)

    @property
    def queued_ask_ids(self) -> tuple[str, ...]:
        return tuple(self._asks)

    def show(self, overlay: events.InteractiveOverlay) -> None:
        """Mount one overlay (an artifact viewer, choices, form, ...)."""
        self._active = overlay

    def show_approval(self, approval: InteractiveApproval) -> None:
        """Mount the highest-priority native approval gate."""
        self._approval = approval

    def clear_approval(self) -> None:
        self._approval = None

    def clear_active(self) -> None:
        self._active = None

    def clear_suspended(self) -> None:
        self._suspended = None

    def request_confirmation(
        self, overlay: InteractiveConfirmation, *, decided: Collection[str], live_input: events.LiveInput | None
    ) -> RequestDisposition:
        """Show the confirmation now, suspend its live view, or queue it.

        The request id is the identity: duplicates never reset the modal and a
        decided request never comes back.
        """
        request_id = overlay.request_id
        if not request_id or request_id in decided:
            return "ignored"
        if isinstance(self._active, InteractiveConfirmation) and self._active.request_id == request_id:
            return "duplicate"
        if self._active is None and self._approval is None:
            self._active = overlay
            return "shown"
        if isinstance(self._active, (InteractiveChoices, InteractiveMarkdown)) and live_input is not None and self._suspended is None:
            self._suspended = SuspendedLive(overlay=self._active, live_input=live_input)
            self._active = overlay
            return "suspended"
        self._confirmations.setdefault(request_id, overlay)
        return "queued"

    def request_ask(self, overlay: InteractiveAsk, *, decided: Collection[str]) -> RequestDisposition:
        """Show the ask now or retain it until the active gate clears."""
        request_id = overlay.request_id
        if not request_id or request_id in decided:
            return "ignored"
        if isinstance(self._active, InteractiveAsk) and self._active.request_id == request_id:
            return "duplicate"
        if self._active is None and self._approval is None:
            self._active = overlay
            return "shown"
        self._asks.setdefault(request_id, overlay)
        return "queued"

    def decide(self, request_id: str) -> bool:
        """Close every overlay bound to the decided request; report a freed focus."""
        self._confirmations.pop(request_id, None)
        self._asks.pop(request_id, None)
        if self._approval is not None and self._approval.request_id == request_id:
            self._approval = None
        if isinstance(self._active, (InteractiveConfirmation, InteractiveAsk)) and self._active.request_id == request_id:
            self._active = None
            return True
        return False

    def retain_revision(self, revision: str) -> bool:
        """Drop queued gates and a suspended view captured under another revision.

        Returns whether the suspended live view was dropped; its polling state
        belongs to the caller.
        """
        for request_id in [request_id for request_id, overlay in self._confirmations.items() if overlay.revision_id != revision]:
            del self._confirmations[request_id]
        for request_id in [request_id for request_id, overlay in self._asks.items() if overlay.revision_id != revision]:
            del self._asks[request_id]
        if self._suspended is not None and self._suspended.overlay.revision_id != revision:
            self._suspended = None
            return True
        return False

    def present(self, *, decided: Collection[str], catalog_revision: str) -> OverlayPresentation:
        """Present the oldest still-pending gate, then restore a suspended view."""
        if self._active is not None or self._approval is not None:
            return NONE_PRESENTED
        while self._confirmations:
            request_id, overlay = next(iter(self._confirmations.items()))
            del self._confirmations[request_id]
            if request_id in decided:
                continue
            self._active = overlay
            return OverlayPresentation("confirmation")
        while self._asks:
            request_id, ask_overlay = next(iter(self._asks.items()))
            del self._asks[request_id]
            if request_id in decided:
                continue
            self._active = ask_overlay
            return OverlayPresentation("ask")
        suspended = self._suspended
        if suspended is None:
            return NONE_PRESENTED
        self._suspended = None
        revision = suspended.overlay.revision_id
        if not revision or revision != catalog_revision:
            return NONE_PRESENTED
        self._active = suspended.overlay
        return OverlayPresentation("live", suspended=suspended)

    def reset_session(self) -> None:
        """Drop per-session overlays; a native approval is closed by its durable facts."""
        self._active = None
        self._confirmations.clear()
        self._asks.clear()
        self._suspended = None


__all__ = [
    "LiveOverlay",
    "NONE_PRESENTED",
    "OverlayPresentation",
    "OverlayStack",
    "PresentationKind",
    "RequestDisposition",
    "SuspendedLive",
]
