"""Narrow read-only view handed to resource-reader handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from voidcode.sdk.resources import ResourceProcessStatus
from voidcode.sdk.services import ArtifactPage, ChildSessionSnapshot, ProcessOutput, ProcessStream

from ..history_renderer import HistoryPage

if TYPE_CHECKING:
    from voidcode.sdk.tools import ToolDefinition

    from ..plugins import LeaseContext, PluginRuntime
    from . import RuntimeServices


class ResourceReaderServicesView:
    """The ``ResourceReaderServices`` protocol over one reader sub-lease.

    A thin wrapper, not a second implementation: narrow read methods delegate
    to the same ``RuntimeServices`` gates (lease liveness, lineage, visibility),
    and the sub-lease itself is read-only with no internal work. Staging,
    spawn, process-control, and network methods do not exist here.
    """

    def __init__(self, runtime: PluginRuntime, lease: LeaseContext) -> None:
        from . import RuntimeServices

        self._services: RuntimeServices = RuntimeServices(runtime, lease)

    @property
    def owner(self) -> str:
        return self._services.owner

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        return self._services.get_state(scope)

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        return self._services.read_artifact(sha256, offset=offset, limit=limit)

    def tool_definition(self, name: str) -> ToolDefinition | None:
        return self._services.tool_definition(name)

    def read_session_history(self, session_id: str, *, after_sequence: int = 0, offset: int = 0, limit: int = 100) -> HistoryPage:
        return self._services.read_session_history(session_id, after_sequence=after_sequence, offset=offset, limit=limit)

    def read_child_snapshot(self, child_session_id: str) -> ChildSessionSnapshot | None:
        return self._services._read_child_snapshot(child_session_id)

    def list_processes(self) -> tuple[ResourceProcessStatus, ...]:
        return self._services._list_resource_processes()

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: ProcessStream = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        return self._services._read_resource_process_output(process_id, stream=stream, offset=offset, limit=limit)
