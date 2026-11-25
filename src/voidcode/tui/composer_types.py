"""Typed composer outcomes and the fixed editor limits shared across modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = ["SHELL_ESCAPE_PREFIX", "ComposerAction", "ComposerOutcome"]

#: The reserved human shell escape; the client strips exactly one prefix before dispatch.
SHELL_ESCAPE_PREFIX: Final[str] = "!"
#: omp ``DOUBLE_INTERRUPT_MS`` (``prompt/composer.ts:22``).
DOUBLE_INTERRUPT_SECONDS = 0.5
#: omp session history depth (``editor.ts:915-919``).
HISTORY_DEPTH = 100


class ComposerAction(StrEnum):
    """What the app should do after a key press."""

    NONE = "none"
    SUBMIT = "submit"
    CANCEL_TURN = "cancel_turn"
    INTERRUPT = "interrupt"
    COPY = "copy"


@dataclass(frozen=True, slots=True)
class ComposerOutcome:
    """Result of :meth:`Composer.handle_key`."""

    action: ComposerAction = ComposerAction.NONE
    text: str = ""
