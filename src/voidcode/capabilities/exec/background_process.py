"""Stable facade for the background process tool modules.

The implementation lives in cohesive sibling modules. This module re-exports the
surface used by plugin registration, the `proc` resource reader, and tests, and
keeps the historical ``background_process._LOG_*``/``_READY_*``/limit
monkeypatch paths working by forwarding bound writes to the shared limits module.
"""

from __future__ import annotations

import sys
import types

from . import background_limits as _limits
from .background_models import (
    BackgroundProcessExternalBody,
    BackgroundProcessListBody,
    BackgroundProcessLogsBody,
    BackgroundProcessRow,
    BackgroundProcessSendBody,
    BackgroundProcessStartBody,
    BackgroundProcessStopBody,
)
from .background_models import (
    ProcessLiveness as ProcessLiveness,
)
from .background_state import (
    _ProcessRecord as _ProcessRecord,
)
from .background_state import (
    _read_records as _read_records,
)
from .background_state import (
    _write_records as _write_records,
)
from .background_state import (
    prepare_exec_deactivation as prepare_exec_deactivation,
)
from .background_tool import BackgroundProcessTool as BackgroundProcessTool

MAX_PROCESS_ROWS = _limits.MAX_PROCESS_ROWS
MAX_SEND_BYTES = _limits.MAX_SEND_BYTES
_LOG_LINE_CHARS = _limits._LOG_LINE_CHARS
_LOG_LINE_SUFFIX = _limits._LOG_LINE_SUFFIX
_LOG_PAGE_BYTES = _limits._LOG_PAGE_BYTES
_LOG_RENDER_BYTES = _limits._LOG_RENDER_BYTES
_LOG_SCAN_BYTES = _limits._LOG_SCAN_BYTES
_READY_POLL_SECONDS = _limits._READY_POLL_SECONDS
_READY_SCAN_BYTES = _limits._READY_SCAN_BYTES

__all__ = [
    "BackgroundProcessExternalBody",
    "BackgroundProcessListBody",
    "BackgroundProcessLogsBody",
    "BackgroundProcessRow",
    "BackgroundProcessSendBody",
    "BackgroundProcessStartBody",
    "BackgroundProcessStopBody",
    "BackgroundProcessTool",
]

_LIMIT_NAMES = frozenset(
    {
        "MAX_PROCESS_ROWS",
        "MAX_SEND_BYTES",
        "_LOG_LINE_CHARS",
        "_LOG_LINE_SUFFIX",
        "_LOG_PAGE_BYTES",
        "_LOG_RENDER_BYTES",
        "_LOG_SCAN_BYTES",
        "_READY_POLL_SECONDS",
        "_READY_SCAN_BYTES",
    }
)


class _BackgroundProcessFacade(types.ModuleType):
    """Keep the documented facade monkeypatch path in sync with shared limits."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in _LIMIT_NAMES:
            setattr(_limits, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _BackgroundProcessFacade
