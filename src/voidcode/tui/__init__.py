"""``voidcode.tui``: the inline terminal client for the runtime.

The renderer is split into pure layers (``term``, ``region``, ``theme``,
``transcript``, ``statusline``, ``composer``, ``overlay``, ``events``) and one app
loop (``app``). Only :func:`run_tui` is exported.

The import is deliberately lazy: ``import voidcode.tui`` must not drag in the
renderer's ``rich`` stack or the runtime for callers that never start the TUI.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..runtime.permission import PermissionDecision
    from ..runtime.service import VoidCodeRuntime

__all__ = ["run_tui"]


def run_tui(
    *,
    workspace: Path,
    approval_mode: PermissionDecision | None = None,
    runtime: VoidCodeRuntime | None = None,
    keymap: Mapping[str, str] | None = None,
) -> int:
    """Run the inline TUI until the user exits; returns the process exit code."""
    from .app import run_tui as run

    return run(
        workspace=workspace,
        approval_mode=approval_mode,
        runtime=runtime,
        keymap=keymap,
    )
