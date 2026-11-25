"""Governed turn execution loop and provider turn streaming.

The driver facade stays here; provider aggregation, turn policy, the drive loop,
terminal outcomes, and host contracts live in private responsibility mixins.
"""

from __future__ import annotations

from ._provider_turn import _tool_call_from_entry as _tool_call_from_entry
from ._provider_turn import _tool_calls_from_content as _tool_calls_from_content
from ._turn_contracts import (
    MODEL_CONFIGURATION_GUIDANCE,
    CoreResult,
    CoreRunStatus,
    TurnExecutionHost,
    TurnExecutionSnapshot,
)
from ._turn_drive import TurnDriveMixin

__all__ = (
    "CoreResult",
    "CoreRunStatus",
    "MODEL_CONFIGURATION_GUIDANCE",
    "TurnDriver",
    "TurnExecutionHost",
    "TurnExecutionSnapshot",
)


class TurnDriver(TurnDriveMixin):
    """Governed turn execution loop and provider turn streaming manager."""
