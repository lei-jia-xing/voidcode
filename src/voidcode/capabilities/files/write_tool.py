"""Workspace file write tool: atomic replace with a stale-hash contract."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    ResourceChange,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

MAX_WRITE_BYTES = 64 * 1024  # Max single-file write 64 KiB to fit comfortably in 1 MiB wire frames


@dataclass(frozen=True, slots=True)
class WriteResultBody:
    path: str
    sha256: str
    bytes: int

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class WriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="write",
        icon=Icon(glyph="✚", fallback="W"),
        description="Write a UTF-8 text file inside the current workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "content": {"type": "string", "maxLength": 65536, "description": "Complete UTF-8 file contents (max 64 KiB)."},
                "expectedHash": {"type": "string", "description": "Required when target file exists: SHA-256 hash of current file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
        replay_policy="never",
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,  # noqa: ARG002 - required by the frozen Tool ABI
    ) -> ToolResult:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="Invocation cancelled before committing file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        workspace = context.require_workspace().resolve()
        raw_path = call.arguments.get("path")
        content = call.arguments.get("content")

        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolFailure(self.definition.name, error="path must be a non-empty string")

        if not isinstance(content, str):
            return ToolFailure(self.definition.name, error="content must be a string")

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_WRITE_BYTES:
            return ToolFailure(
                self.definition.name,
                error=f"Content exceeds size limit ({len(content_bytes)} bytes > {MAX_WRITE_BYTES} bytes)",
            )

        p = Path(raw_path)
        try:
            candidate = p.resolve() if p.is_absolute() else (workspace / p).resolve()
        except Exception as exc:
            return ToolFailure(self.definition.name, error=f"Invalid path: {exc}")

        if not candidate.is_relative_to(workspace):
            return ToolFailure(self.definition.name, error=f"Path escapes workspace: {raw_path}")

        if candidate.is_dir():
            return ToolFailure(self.definition.name, error=f"Cannot write to directory: {raw_path}")

        raw_target = p if p.is_absolute() else (workspace / p)
        if raw_target.is_symlink() or candidate.is_symlink():
            return ToolFailure(self.definition.name, error=f"Cannot write to symlink: {raw_path}")

        initial_stat: os.stat_result | None = None
        initial_hash: str | None = None
        target_mode = 0o644

        if candidate.exists():
            expected_hash = call.arguments.get("expectedHash")
            if not isinstance(expected_hash, str) or not expected_hash:
                return ToolFailure(
                    self.definition.name,
                    error="write requires an expectedHash argument when overwriting an existing file",
                )
            initial_stat = candidate.stat()
            target_mode = initial_stat.st_mode
            initial_hash = _hash_file(candidate)
            if expected_hash != initial_hash:
                return ToolFailure(
                    self.definition.name,
                    error=f"Stale write rejected: expected hash {expected_hash} does not match current hash {initial_hash}",
                )

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="Invocation cancelled before committing file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        candidate.parent.mkdir(parents=True, exist_ok=True)
        if not candidate.parent.resolve().is_relative_to(workspace):
            return ToolFailure(self.definition.name, error=f"Parent directory escapes workspace: {raw_path}")

        temp_fd, temp_path_str = tempfile.mkstemp(
            prefix=".voidcode_write_",
            dir=candidate.parent,
        )
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(content_bytes)
                f.flush()
                os.fsync(f.fileno())

            os.chmod(temp_path, target_mode)

            if context.abort_signal is not None and context.abort_signal.cancelled:
                return ToolFailure(
                    self.definition.name,
                    error="Invocation cancelled before committing file",
                    execution=ConfirmedStop(cancellation_signalled=True),
                )

            # Recheck path identity and content hash at replace boundary to reject concurrent modification
            if initial_stat is not None:
                if not candidate.exists():
                    return ToolFailure(self.definition.name, error=f"File deleted concurrently during write: {raw_path}")
                cur_stat = candidate.stat()
                if cur_stat.st_ino != initial_stat.st_ino or _hash_file(candidate) != initial_hash:
                    return ToolFailure(self.definition.name, error=f"File modified concurrently during write: {raw_path}")
            else:
                if candidate.exists():
                    return ToolFailure(self.definition.name, error=f"File created concurrently during write: {raw_path}")

            os.replace(temp_path, candidate)
        except Exception as exc:
            return ToolFailure(self.definition.name, error=f"Write failed: {exc}")
        finally:
            temp_path.unlink(missing_ok=True)

        written_hash = hashlib.sha256(content_bytes).hexdigest()
        display_path = candidate.relative_to(workspace).as_posix()
        output_text = f"Wrote {len(content_bytes)} byte(s) to {display_path} (SHA-256: {written_hash})"

        body = WriteResultBody(
            path=display_path,
            sha256=written_hash,
            bytes=len(content_bytes),
        )
        return ToolSuccess(
            self.definition.name,
            output=TextOutput(output_text),
            body=body,
            changes=(ResourceChange(display_path, "create" if initial_stat is None else "update"),),
        )
