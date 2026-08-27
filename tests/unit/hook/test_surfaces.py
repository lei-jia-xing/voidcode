from __future__ import annotations

from typing import cast

import pytest

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.surfaces import (
    HOOK_SURFACE_DESCRIPTORS,
    RuntimeHookSurface,
    hook_surface_descriptor,
)

_EXPECTED_SURFACES = (
    "pre_tool",
    "post_tool",
    "session_start",
    "session_end",
    "session_idle",
    "background_task_registered",
    "background_task_started",
    "background_task_progress",
    "background_task_completed",
    "background_task_failed",
    "background_task_cancelled",
    "background_task_interrupted",
    "background_task_notification_enqueued",
    "background_task_result_read",
    "delegated_result_available",
    "turn_progress",
    "stuck_detected",
)


def test_hook_surface_catalog_is_complete_and_unique() -> None:
    assert tuple(descriptor.surface for descriptor in HOOK_SURFACE_DESCRIPTORS) == _EXPECTED_SURFACES
    assert len({descriptor.surface for descriptor in HOOK_SURFACE_DESCRIPTORS}) == len(_EXPECTED_SURFACES)
    assert all(descriptor.event_type.startswith("runtime.") for descriptor in HOOK_SURFACE_DESCRIPTORS)


def test_hook_surface_descriptor_carries_config_and_phase_metadata() -> None:
    assert hook_surface_descriptor("pre_tool").config_attribute == "pre_tool"
    assert hook_surface_descriptor("session_start").config_attribute == "on_session_start"
    assert hook_surface_descriptor("background_task_result_read").phase == "background"
    assert hook_surface_descriptor("turn_progress").phase == "foreground"

    hooks = RuntimeHooksConfig(
        pre_tool=(("pre",),),
        on_session_start=(("start",),),
        on_background_task_result_read=(("result",),),
    )
    assert hooks.commands_for_surface("pre_tool") == (("pre",),)
    assert hooks.commands_for_surface("session_start") == (("start",),)
    assert hooks.commands_for_surface("background_task_result_read") == (("result",),)


def test_hook_surface_descriptor_preserves_unknown_surface_failure() -> None:
    with pytest.raises(KeyError):
        hook_surface_descriptor("does_not_exist")
    with pytest.raises(KeyError):
        RuntimeHooksConfig().commands_for_surface(cast(RuntimeHookSurface, "does_not_exist"))
