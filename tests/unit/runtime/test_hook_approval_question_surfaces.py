"""Catalog + config coverage for the approval/question foreground hook surfaces.

Scenario S3: ``approval_requested`` and ``question_asked`` are fail-open
foreground observers. They reuse the existing ``runtime.approval_requested``
and ``runtime.question_requested`` event names -- no new event vocabulary.
"""

from __future__ import annotations

import pytest

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.surfaces import hook_surface_descriptor
from voidcode.runtime.config_models import HOOK_COMMAND_FIELDS, HooksPayload


def test_hook_surface_descriptor_for_unknown_surface_still_key_error() -> None:
    with pytest.raises(KeyError):
        hook_surface_descriptor("nope_not_a_surface")


def test_runtime_hooks_config_exposes_new_argv_slots() -> None:
    config = RuntimeHooksConfig(
        on_approval_requested=(("echo", "approval"),),
        on_question_asked=(("echo", "question"),),
    )
    assert config.commands_for_surface("approval_requested") == (("echo", "approval"),)
    assert config.commands_for_surface("question_asked") == (("echo", "question"),)
    assert RuntimeHooksConfig().on_approval_requested == ()
    assert RuntimeHooksConfig().on_question_asked == ()


def test_hooks_payload_schema_mirror_exposes_new_argv_slots() -> None:
    payload = HooksPayload.model_validate(
        {
            "on_approval_requested": [["echo", "approval"]],
            "on_question_asked": [["echo", "question"]],
        }
    )
    assert payload.on_approval_requested == (("echo", "approval"),)
    assert payload.on_question_asked == (("echo", "question"),)
    assert "on_approval_requested" in HOOK_COMMAND_FIELDS
    assert "on_question_asked" in HOOK_COMMAND_FIELDS
