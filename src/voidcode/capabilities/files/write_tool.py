"""Workspace file write tool: atomic replace with a stale-hash contract."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.atomic_file import replace_file
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from ..tool_errors import expected_failures, raise_tool_diagnostic
from ._common import (
    MAX_MUTATION_CONTENT_BYTES,
    PathEscapesWorkspaceError,
    make_file_change,
    resolve_workspace_path,
    summarize_diff,
)
from .mutation import enforce_content_bound, invalid_path_failure, literal_target, outside_workspace_failure, raise_symlink_target


@dataclass(frozen=True, slots=True)
class WriteResultBody:
    path: str
    sha256: str
    bytes: int
    diff: str | None = None
    additions: int | None = None
    deletions: int | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }
        if self.diff is not None:
            payload["diff"] = self.diff
            payload["additions"] = self.additions
            payload["deletions"] = self.deletions
        return payload


class _WriteArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, description="Path relative to the workspace.")
    content: str = Field(description="Complete UTF-8 file contents (at most 64 KiB of UTF-8 bytes).")
    expectedHash: str | None = Field(
        default=None,
        description="Required when the target file exists: SHA-256 of the current file bytes, as reported by read.",
    )


class _WriteCancelled(Exception):
    """Cancellation observed between preparing and committing a sibling file."""


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _raise_if_cancelled(context: ToolContext) -> None:
    if context.abort_signal is not None and context.abort_signal.cancelled:
        raise _WriteCancelled


def _commit_overwrite(
    candidate: Path,
    content_bytes: bytes,
    *,
    initial_stat: os.stat_result,
    initial_hash: str,
    context: ToolContext,
    display_path: str,
) -> str | None:
    """Replace an existing file atomically; return its previous text when small enough for a diff."""
    temp_fd, temp_path_str = tempfile.mkstemp(prefix=".voidcode_write_", dir=candidate.parent)
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(temp_fd, "wb") as stream:
            stream.write(content_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, initial_stat.st_mode)

        _raise_if_cancelled(context)

        if not candidate.exists():
            raise_tool_diagnostic(
                message=f"write rejected because the target was deleted concurrently: {display_path}",
                error_kind="stale_edit",
                reason="commit_race",
                retry_guidance="Read the target path again, then retry the write against the current state.",
                details={"path": display_path},
            )
        current = candidate.stat()
        if current.st_ino != initial_stat.st_ino:
            raise_tool_diagnostic(
                message=f"write rejected because the target was replaced concurrently: {display_path}",
                error_kind="stale_edit",
                reason="commit_race",
                retry_guidance="Read the target path again, then retry the write against the current file.",
                details={"path": display_path},
            )
        # Stream the recheck so a large existing file never has to fit in memory.
        if _hash_file(candidate) != initial_hash:
            raise_tool_diagnostic(
                message=f"write rejected because the target changed concurrently: {display_path}",
                error_kind="stale_edit",
                reason="commit_race",
                retry_guidance="Read the target path again, then retry the write with the current SHA-256 as expectedHash.",
                details={"path": display_path},
            )
        before_text: str | None = None
        if current.st_size <= MAX_MUTATION_CONTENT_BYTES:
            before_text = candidate.read_bytes().decode("utf-8", errors="replace")
        os.replace(temp_path, candidate)
        return before_text
    finally:
        temp_path.unlink(missing_ok=True)


class WriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="write",
        icon=Icon(glyph="✚"),
        description=(
            "Write a UTF-8 text file inside the current workspace. Creating a file needs no hash; "
            "overwriting needs the current SHA-256 as expectedHash."
        ),
        input_schema=_WriteArgs.model_json_schema(),
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
        replay_policy="never",
    )

    @expected_failures
    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,  # noqa: ARG002 - required by the frozen Tool ABI
    ) -> ToolResult:
        args = _WriteArgs.model_validate(dict(call.arguments))
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="write cancelled before committing the file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        workspace = context.require_workspace().resolve()
        content_bytes = args.content.encode("utf-8")
        enforce_content_bound(len(content_bytes), tool_name=self.definition.name)

        try:
            candidate, display_path = resolve_workspace_path(workspace, args.path, allow_outside_workspace=False)
        except PathEscapesWorkspaceError:
            return outside_workspace_failure(self.definition.name, args.path)
        except (OSError, ValueError) as exc:
            return invalid_path_failure(self.definition.name, args.path, detail=str(exc))

        if candidate.is_dir():
            raise_tool_diagnostic(
                message=f"write refuses to overwrite a directory: {args.path}",
                error_kind="tool_input_mismatch",
                reason="not_a_regular_file",
                retry_guidance="Retry with a file path; writing a file inside a directory creates that file.",
                details={"path": display_path},
            )
        if literal_target(workspace, args.path).is_symlink():
            raise_symlink_target(self.definition.name, args.path)

        initial_stat: os.stat_result | None = None
        initial_hash = ""
        try:
            if candidate.exists():
                if not args.expectedHash:
                    raise_tool_diagnostic(
                        message="write requires an expectedHash argument when overwriting an existing file",
                        error_kind="tool_input_mismatch",
                        reason="expected_hash_required",
                        retry_guidance="Read the file again (or call read with hash_only=true), then retry with its SHA-256 as expectedHash.",
                        details={"path": display_path},
                    )
                initial_stat = candidate.stat()
                initial_hash = _hash_file(candidate)
        except OSError as exc:
            raise_tool_diagnostic(
                message=f"write failed while inspecting {display_path}: {exc}",
                error_kind="tool_execution_error",
                reason="write_failed",
                retry_guidance="Retry the write; if the failure persists, check the target's permissions.",
                details={"path": display_path},
            )
        if initial_stat is not None and args.expectedHash != initial_hash:
            raise_tool_diagnostic(
                message=f"Stale write rejected: expected hash {args.expectedHash} does not match current hash {initial_hash}",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again (or call read with hash_only=true), use its SHA-256, then retry the write.",
                details={"path": display_path, "expected_hash": args.expectedHash, "actual_hash": initial_hash},
            )

        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="write cancelled before committing the file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )

        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            parent_inside_workspace = candidate.parent.resolve().is_relative_to(workspace)
        except OSError as exc:
            raise_tool_diagnostic(
                message=f"write failed while preparing {display_path}: {exc}",
                error_kind="tool_execution_error",
                reason="write_failed",
                retry_guidance="Retry the write; if the failure persists, check the target's parent directory permissions.",
                details={"path": display_path},
            )
        if not parent_inside_workspace:
            raise_tool_diagnostic(
                message=f"write refuses a parent directory outside the workspace: {args.path}",
                error_kind="tool_input_mismatch",
                reason="outside_workspace",
                retry_guidance="Retry with a workspace-relative path; read, glob, and grep may still access external files.",
                details={"path": args.path},
            )

        before_text: str | None = None
        try:
            if initial_stat is None:
                # ``os.link`` inside replace_file makes creation exclusive: a concurrently
                # created target fails instead of being clobbered by a check-then-replace.
                replace_file(candidate, content_bytes, expected_content=None)
            else:
                before_text = _commit_overwrite(
                    candidate,
                    content_bytes,
                    initial_stat=initial_stat,
                    initial_hash=initial_hash,
                    context=context,
                    display_path=display_path,
                )
        except _WriteCancelled:
            return ToolFailure(
                self.definition.name,
                error="write cancelled before committing the file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        except FileExistsError:
            raise_tool_diagnostic(
                message=f"write rejected because the target was created concurrently: {display_path}",
                error_kind="stale_edit",
                reason="commit_race",
                retry_guidance="Read the target path, then retry with its current SHA-256 as expectedHash.",
                details={"path": display_path},
            )
        except OSError as exc:
            raise_tool_diagnostic(
                message=f"write failed while committing {display_path}: {exc}",
                error_kind="tool_execution_error",
                reason="write_failed",
                retry_guidance="Retry the write; if the failure persists, check the target's parent directory permissions and free space.",
                details={"path": display_path},
            )

        written_hash = hashlib.sha256(content_bytes).hexdigest()
        output_text = f"Wrote {len(content_bytes)} byte(s) to {display_path} (SHA-256: {written_hash})"
        diff_text: str | None = None
        additions: int | None = None
        deletions: int | None = None
        if before_text is not None and before_text != args.content:
            diff_text, additions, deletions = summarize_diff(path=display_path, before=before_text, after=args.content)

        body = WriteResultBody(
            path=display_path,
            sha256=written_hash,
            bytes=len(content_bytes),
            diff=diff_text,
            additions=additions,
            deletions=deletions,
        )
        return ToolSuccess(
            self.definition.name,
            output=TextOutput(output_text),
            body=body,
            changes=(make_file_change(display_path, "create" if initial_stat is None else "update", content_hash=written_hash),),
        )
