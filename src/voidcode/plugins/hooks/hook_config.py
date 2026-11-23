"""User argv hook configuration and the tool-name glob seam."""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass

#: Hard ceiling for one configured hook command. The executor also clamps, so a
#: directly constructed config can never turn into an unbounded wait.
MAX_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class RuntimeHooksConfig:
    enabled: bool | None = True
    timeout_seconds: float = 30.0
    pre_tool: tuple[tuple[str, ...], ...] = ()
    #: Optional tool-name glob filters for the tool-hook surfaces. Empty means
    #: "match every tool" (backcompat); non-empty filters with stdlib fnmatch.
    pre_tool_match: tuple[str, ...] = ()
    post_tool: tuple[tuple[str, ...], ...] = ()
    post_tool_match: tuple[str, ...] = ()
    on_session_start: tuple[tuple[str, ...], ...] = ()
    on_session_end: tuple[tuple[str, ...], ...] = ()


def hook_tool_matches(patterns: Sequence[str], tool_name: str) -> bool:
    """Return True when ``tool_name`` passes the surface glob filter.

    Wired into ``run_tool_hooks`` for the ``pre_tool``/``post_tool`` surfaces;
    that loop calls this with the surface's ``*_match`` tuple before running
    any command. Empty patterns match all (backcompat), otherwise stdlib
    ``fnmatch.fnmatchcase`` applies and is case-sensitive.
    """
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns)


__all__ = [
    "MAX_TIMEOUT_SECONDS",
    "RuntimeHooksConfig",
    "hook_tool_matches",
]
