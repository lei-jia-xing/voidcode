from __future__ import annotations

from typing import NoReturn


class ToolDiagnosticError(ValueError):
    def __init__(
        self,
        *,
        message: str,
        error_kind: str,
        error_details: dict[str, object] | None = None,
        retry_guidance: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_kind = error_kind
        self.error_details = dict(error_details or {})
        self.retry_guidance = retry_guidance


def raise_tool_diagnostic(
    *,
    message: str,
    error_kind: str,
    reason: str,
    retry_guidance: str | None = None,
    details: dict[str, object] | None = None,
) -> NoReturn:
    payload: dict[str, object] = {"reason": reason}
    if details:
        payload.update(details)
    raise ToolDiagnosticError(
        message=message,
        error_kind=error_kind,
        error_details=payload,
        retry_guidance=retry_guidance,
    )
