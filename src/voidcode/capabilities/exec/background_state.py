"""Persisted background-process identities and their owner-state read/write."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from voidcode.capabilities.tool_errors import raise_tool_diagnostic
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.resources import ResourceReaderServices
from voidcode.sdk.services import ProcessHandle, ProcessServices, StateServices

from .args import NonEmptyCommand, NonEmptyProcessId
from .background_models import ProcessLiveness

#: Current persisted process-record payload version; unsupported versions fail visibly.
_STATE_VERSION = 1


class _ProcessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    process_id: NonEmptyProcessId
    origin_session_id: str | None = Field(default=None, min_length=1)
    pid: int = Field(gt=0)
    start_time: str = Field(min_length=1)
    description: str
    command: NonEmptyCommand
    stdout_path: str = Field(min_length=1)
    stderr_path: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    state: ProcessLiveness
    exit_code: int | None = None


class _ProcessState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    schema_version: int = Field(ge=1, le=1)
    processes: dict[str, _ProcessRecord]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_records(services: StateServices | ResourceReaderServices) -> tuple[dict[str, _ProcessRecord], int]:
    """Validate persisted identities once; malformed state is never an empty inventory."""
    state, version = services.get_state("workspace")
    if state is None:
        return {}, version
    try:
        parsed = _ProcessState.model_validate(json_wire_object(state))
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<state>"
        raise_tool_diagnostic(
            message=f"background process state is unreadable: {location}: {first.get('msg', 'invalid value')}",
            error_kind="tool_execution_error",
            reason="corrupt_process_state",
            retry_guidance="The persisted process inventory is malformed; repair or reset workspace state before retrying.",
            details={"error": str(first.get("msg", "invalid value"))},
        )
    for process_id, record in parsed.processes.items():
        if record.process_id != process_id:
            raise_tool_diagnostic(
                message="background process state is unreadable: a record id does not match its state key",
                error_kind="tool_execution_error",
                reason="corrupt_process_state",
                retry_guidance="The persisted process inventory is malformed; repair or reset workspace state before retrying.",
            )
    return parsed.processes, version


def _write_records(services: StateServices, records: Mapping[str, _ProcessRecord], version: int) -> int:
    return services.replace_state(
        {"schema_version": _STATE_VERSION, "processes": {name: record.model_dump(exclude_unset=True) for name, record in records.items()}},
        expected_version=version,
        scope="workspace",
    )


def prepare_exec_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: persisted process identities survive untouched as history.

    Records are identities (pid + start time + log paths), not supervision handles: the host
    owns live processes and supervised background jobs survive a generation swap by design.
    Disabling exec only drops the control surface until re-enable, when the same identities
    re-attach to still-supervised ids or report honestly as externally managed. Nothing here
    stops a process, and a running process never makes exec undisablable.
    """
    _ = (scope, scope_key, tip_sequence)
    return state


def _with_exit(record: _ProcessRecord, *, exit_code: int | None) -> _ProcessRecord:
    """Preserve a known historical exit code when the host has no new answer."""
    if exit_code is None:
        return record.model_copy(update={"state": "exited"})
    return record.model_copy(update={"state": "exited", "exit_code": exit_code})


def _identity_record(*, handle: ProcessHandle, command: str, description: str, origin_session_id: str) -> _ProcessRecord:
    record = _ProcessRecord(
        process_id=handle.process_id,
        origin_session_id=origin_session_id,
        pid=handle.pid,
        start_time=handle.start_time,
        description=description,
        command=command,
        stdout_path=handle.stdout_path,
        stderr_path=handle.stderr_path,
        started_at=_now(),
        state="running",
    )
    if handle.exit_code is not None:
        record.exit_code = handle.exit_code
    return record


def _live_handle(services: ProcessServices, process_id: str) -> ProcessHandle | None:
    """This session's handle for one process; `None` when the host does not supervise that id."""
    for handle in services.list_processes():
        if handle.process_id == process_id:
            return handle
    return None
