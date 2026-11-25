"""Lifecycle transform for disabling the subagents plugin."""

from __future__ import annotations

from collections.abc import Mapping


def prepare_subagents_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Child/group mappings survive untouched as history.

    Children are core-owned, not leases; running children block the switch, and the
    mappings resume control on re-enable.
    """
    _ = (scope, scope_key, tip_sequence)
    return state
