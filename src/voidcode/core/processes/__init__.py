"""Child process management: process groups, supervised services, and background jobs.

The facade exports the stable process-service surface; the lifecycle lives in private
modules: capture/streams, procfs signaling, supervised entries, one-shot exec, the
core-lifetime host, and the generation registry.
"""

from __future__ import annotations

from voidcode.sdk.services import ProcessResult

from ._host import ProcessHost
from ._registry import ProcessRegistry

__all__ = ("ProcessHost", "ProcessRegistry", "ProcessResult")
