from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ....security.json_values import json_wire_object, own_json_object


@dataclass(frozen=True, slots=True)
class BackgroundProcessRow:
    process_id: str
    pid: int
    command: str
    cwd: str
    status: str
    running: bool | None
    exit_code: int | None
    prior_runtime: bool
    observed_running: bool | None
    identity_match: bool | None
    controllable: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "command": self.command,
            "cwd": self.cwd,
            "status": self.status,
            "running": self.running,
            "exit_code": self.exit_code,
            "prior_runtime": self.prior_runtime,
            "observed_running": self.observed_running,
            "identity_match": self.identity_match,
            "controllable": self.controllable,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessListBody:
    processes: tuple[BackgroundProcessRow, ...]
    count: int
    limit: int

    def as_payload(self) -> dict[str, object]:
        return {
            "processes": [process.as_payload() for process in self.processes],
            "count": self.count,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessStaleBody:
    process_id: str
    pid: int
    status: str
    prior_runtime: bool
    reconciliation_reason: str | None
    running: None
    observed_running: bool | None
    identity_match: bool | None
    controllable: bool

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "process_id": self.process_id,
            "pid": self.pid,
            "status": self.status,
            "prior_runtime": self.prior_runtime,
            "running": self.running,
            "observed_running": self.observed_running,
            "identity_match": self.identity_match,
            "controllable": self.controllable,
        }
        if self.reconciliation_reason is not None:
            payload["reconciliation_reason"] = self.reconciliation_reason
        return payload


@dataclass(frozen=True, slots=True)
class BackgroundProcessStartBody:
    process_id: str
    pid: int
    command: str
    cwd: str
    running: bool
    reused: bool
    stale_process_id: str | None
    guidance: str

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "command": self.command,
            "cwd": self.cwd,
            "running": self.running,
            "reused": self.reused,
            "stale_process_id": self.stale_process_id,
            "guidance": self.guidance,
        }


@dataclass(frozen=True, slots=True)
class BackgroundProcessSendBody:
    process_id: str
    input: str
    newline: bool

    def as_payload(self) -> dict[str, object]:
        return {"process_id": self.process_id, "input": self.input, "newline": self.newline}


@dataclass(frozen=True, slots=True)
class BackgroundProcessStopBody:
    process_id: str
    exit_code: int | None
    running: bool

    def as_payload(self) -> dict[str, object]:
        return {"process_id": self.process_id, "exit_code": self.exit_code, "running": self.running}


@dataclass(frozen=True, slots=True)
class BackgroundProcessLogsBody:
    process_id: str
    status: str
    prior_runtime: bool
    reconciliation_reason: str | None
    running: bool
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_retained_lines: int
    stderr_retained_lines: int
    stdout_dropped_lines: int
    stderr_dropped_lines: int
    stdout_artifact: Mapping[str, object] | None
    stderr_artifact: Mapping[str, object] | None
    truncated: bool
    references: tuple[str, ...]
    guidance: str

    def __post_init__(self) -> None:
        for name in ("stdout_artifact", "stderr_artifact"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, own_json_object(value))

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "status": self.status,
            "prior_runtime": self.prior_runtime,
            "reconciliation_reason": self.reconciliation_reason,
            "running": self.running,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_retained_lines": self.stdout_retained_lines,
            "stderr_retained_lines": self.stderr_retained_lines,
            "stdout_dropped_lines": self.stdout_dropped_lines,
            "stderr_dropped_lines": self.stderr_dropped_lines,
            "stdout_artifact": None if self.stdout_artifact is None else json_wire_object(self.stdout_artifact),
            "stderr_artifact": None if self.stderr_artifact is None else json_wire_object(self.stderr_artifact),
            "truncated": self.truncated,
            "references": list(self.references),
            "guidance": self.guidance,
        }
