"""Pydantic operation arguments for `background_process`."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .args import NonEmptyCommand, NonEmptyProcessId, OptionalDescription
from .background_limits import MAX_SEND_BYTES


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _StartArgs(_StrictArgs):
    op: Literal["start"] = Field(description="Start a supervised process that outlives this call.")
    command: NonEmptyCommand = Field(description="Shell command to run in the workspace for the lifetime of the process.")
    description: OptionalDescription = Field(default=None, description="Human-readable description of the process, persisted with its identity.")
    ready_pattern: str | None = Field(
        default=None, min_length=1, max_length=512, description="Text to wait for in the process output before returning."
    )
    ready_timeout: int = Field(default=10, ge=1, le=30, description="Seconds to wait for ready_pattern (1-30, default 10).")


class _PsArgs(_StrictArgs):
    op: Literal["ps"] = Field(description="List the processes of this workspace and session.")


class _ProcessIdArgs(_StrictArgs):
    process_id: NonEmptyProcessId = Field(description="Process id returned by op=start.")


class _LogsArgs(_ProcessIdArgs):
    op: Literal["logs"] = Field(description="Read a bounded, line-paged window of a process's output.")
    stream: Literal["stdout", "stderr", "both"] = Field(default="both", description="Which output stream to read.")
    offset: int = Field(default=0, ge=0, description="Number of leading lines to skip; page from next_offset to read on.")
    limit: int = Field(default=500, ge=1, le=500, description="Maximum lines to return (1-500).")


class _SendArgs(_ProcessIdArgs):
    op: Literal["send"] = Field(description="Write to a process's standard input.")
    input: str = Field(min_length=1, max_length=MAX_SEND_BYTES, description="Text written to the process's standard input.")
    newline: bool = Field(default=True, description="Append a newline to input (default true).")


class _StopArgs(_ProcessIdArgs):
    op: Literal["stop"] = Field(description="Stop a process and its whole process group.")


_ProcessArgs = Annotated[_StartArgs | _PsArgs | _LogsArgs | _SendArgs | _StopArgs, Field(discriminator="op")]
_ARGS_ADAPTER = TypeAdapter(_ProcessArgs)
