"""Reject forbidden hook-preset authority actions (proof-gate S3).

``validate_hook_preset_actions`` must refuse forbidden entries of
``_FORBIDDEN_HOOK_PRESET_ACTIONS`` in ``src/voidcode/hook/presets.py``.
Two representative cases pin the mechanism, not the list.
"""

from __future__ import annotations

import pytest

from voidcode.hook.presets import validate_hook_preset_actions


@pytest.mark.parametrize(
    "action",
    (
        "bypass_approval",
        "grant_tools",
    ),
)
def test_forbidden_preset_action_rejected(action: str) -> None:
    with pytest.raises(ValueError, match="forbidden authority action"):
        validate_hook_preset_actions((action,), field_path="actions")
