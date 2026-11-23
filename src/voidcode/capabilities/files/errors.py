from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from typing import NoReturn

from pydantic import ValidationError

from voidcode.sdk.tools import TextOutput, ToolCall, ToolDiagnostics, ToolFailure, ToolResult


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


def _call_from(args: Sequence[object], kwargs: Mapping[str, object]) -> ToolCall | None:
    candidate = kwargs.get("call")
    if isinstance(candidate, ToolCall):
        return candidate
    for arg in args:
        if isinstance(arg, ToolCall):
            return arg
    return None


def _validation_failure(tool_name: str, exc: ValidationError) -> ToolFailure:
    errors: list[dict[str, str]] = []
    for item in exc.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<arguments>"
        errors.append(
            {
                "loc": location,
                "msg": str(item.get("msg", "invalid value")),
                "type": str(item.get("type", "value_error")),
            }
        )
    first = errors[0]
    message = f"{tool_name} rejected invalid arguments: {first['loc']}: {first['msg']}"
    if len(errors) > 1:
        message += f" (+{len(errors) - 1} more)"
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(text=message),
        diagnostics=ToolDiagnostics(
            kind="tool_input_validation",
            summary=message,
            details={"reason": "invalid_arguments", "errors": errors[:8]},
            guidance="Retry with arguments that satisfy the tool input schema.",
        ),
    )


def expected_failures[**P](invoke: Callable[P, ToolResult]) -> Callable[P, ToolResult]:
    """Turn expected public-tool failures into typed results; programming errors still raise.

    Only a shared file diagnostic and pydantic argument validation are expected
    boundary results. Bare ``ValueError``, ``OSError``, and every other exception
    remain programming/infrastructure errors and propagate unchanged.
    """

    @wraps(invoke)
    def boundary(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        call = _call_from(args, kwargs)
        tool_name = call.tool_name if call is not None else getattr(invoke, "__name__", "tool")
        try:
            return invoke(*args, **kwargs)
        except ValidationError as exc:
            return _validation_failure(tool_name, exc)
        except ToolDiagnosticError as exc:
            return ToolFailure(
                tool_name=tool_name,
                error=exc.message,
                output=TextOutput(text=exc.message),
                diagnostics=ToolDiagnostics(
                    kind=exc.error_kind,
                    summary=exc.message,
                    details=exc.error_details,
                    guidance=exc.retry_guidance,
                ),
            )

    return boundary


__all__ = ["ToolDiagnosticError", "expected_failures", "raise_tool_diagnostic"]
