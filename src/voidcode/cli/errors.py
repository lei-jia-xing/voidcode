"""Typed CLI failures that carry an explicit process exit code."""

from __future__ import annotations


class CliError(Exception):
    """Typed CLI error carrying an explicit exit code and message."""

    def __init__(self, *, code: int = 1, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
