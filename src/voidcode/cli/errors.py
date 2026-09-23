"""Typed CLI failures that carry an explicit process exit code."""

from __future__ import annotations

from pathlib import Path

from ..cli_support import EXIT_INVALID_RESOURCE


class CliError(Exception):
    """Typed CLI error carrying an explicit exit code and message."""

    def __init__(self, *, code: int = 1, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def require_workspace(workspace: Path) -> None:
    """Reject a workspace path that is not an existing directory."""
    if not workspace.exists() or not workspace.is_dir():
        raise CliError(code=EXIT_INVALID_RESOURCE, message=f"workspace does not exist: {workspace}")
