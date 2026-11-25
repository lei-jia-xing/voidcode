"""Direct in-process plugin services: the PluginServices protocol over core state.

One `RuntimeServices` instance is bound to one lease (owner + session + stance) for the
duration of a single plugin callback. Artifact spills publish immutable byte streams;
process management delegates to the generation `ProcessRegistry` and the shared core-lifetime
`ProcessHost`.

The facade is assembled from private concern mixins: lease gates, process access, artifacts,
staged state, reads, session projections, wakeups, child sessions, background actions, and
governed invocation. Callers keep using `RuntimeServices`; the mixins are not a second API.
"""

from __future__ import annotations

from ._artifacts import ArtifactServices
from ._background import BackgroundServices
from ._children import ChildServices
from ._invocation import InvocationServices, SuspendOutsideTool
from ._lease import ServiceLease
from ._processes import ProcessServices
from ._reader_view import ResourceReaderServicesView
from ._reads import ReadServices
from ._session import SessionServices
from ._state import StateServices
from ._wakeups import WakeupServices

__all__ = ("ResourceReaderServicesView", "RuntimeServices", "SuspendOutsideTool")


class RuntimeServices(
    ProcessServices,
    ArtifactServices,
    StateServices,
    ReadServices,
    SessionServices,
    WakeupServices,
    ChildServices,
    BackgroundServices,
    InvocationServices,
    ServiceLease,
):
    """`PluginServices` bound directly to one lease, calling the runtime directly."""
