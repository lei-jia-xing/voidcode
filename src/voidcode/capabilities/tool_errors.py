"""The expected public-tool failure boundary shared by bundled capabilities.

A tool converts failures it expects from its own inputs or environment into
``ToolDiagnosticError``; :func:`expected_failures` turns those, and pydantic
argument validation, into typed results. Bare ``ValueError``, ``OSError``, and
every other exception remain programming or infrastructure errors and propagate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from typing import Final, Literal, NoReturn, TypeIs, get_args

from pydantic import ValidationError

from voidcode.sdk.tools import TextOutput, ToolCall, ToolDiagnosticKind, ToolDiagnostics, ToolFailure, ToolResult

#: Every reason a bundled tool attaches to its typed diagnostic details. The wire
#: ``details`` object stays open; this alias types the value tools branch on.
type ToolDiagnosticReason = Literal[
    "ambiguous_match",
    "byte_arguments_require_resource_uri",
    "byte_limit_without_byte_offset",
    "character_mid_surrogate",
    "character_mid_utf8",
    "character_out_of_range",
    "content_hash_mismatch",
    "corrupt_process_state",
    "document_answer_shape_violation",
    "document_content_mismatch",
    "document_version_mismatch",
    "document_version_unavailable",
    "duplicate_spawn",
    "edit_failed",
    "empty_content",
    "end_line_out_of_range",
    "failed_process_start",
    "file_changed_during_recovery",
    "hash_only_uri",
    "hash_only_with_page_arguments",
    "identical_old_and_new",
    "ignore_file_read_failed",
    "input_too_large",
    "invalid_arguments",
    "invalid_path",
    "invalid_regex",
    "invalid_workspace",
    "line_too_long",
    "mixed_line_endings",
    "nested_tool_failure",
    "not_a_regular_file",
    "not_text",
    "offset_out_of_range",
    "old_string_not_found",
    "outside_workspace",
    "overlapping_edits",
    "partial_stdin_write",
    "process_capture_failed",
    "process_not_supervised",
    "process_start_failed",
    "range_out_of_bounds",
    "range_reversed",
    "sandbox_helper_failed",
    "search_io_error",
    "search_path_not_directory",
    "search_path_not_found",
    "send_cancelled",
    "start_line_out_of_range",
    "stdin_unavailable",
    "stop_unconfirmed",
    "target_not_found",
    "unknown_process_id",
    "unsupported_encoding",
    "unsupported_resource_uri",
]

_TOOL_DIAGNOSTIC_REASONS: Final[frozenset[str]] = frozenset(value for value in get_args(ToolDiagnosticReason.__value__) if isinstance(value, str))


def is_tool_diagnostic_reason(value: object) -> TypeIs[ToolDiagnosticReason]:
    """Whether a detail payload's ``reason`` names one bundled reason."""
    return isinstance(value, str) and value in _TOOL_DIAGNOSTIC_REASONS


class ToolDiagnosticError(ValueError):
    def __init__(
        self,
        *,
        message: str,
        error_kind: ToolDiagnosticKind,
        error_details: dict[str, object] | None = None,
        retry_guidance: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_kind = error_kind
        self.error_details = dict(error_details or {})
        self.retry_guidance = retry_guidance

    @property
    def reason(self) -> ToolDiagnosticReason | None:
        """The typed reason discriminator, or ``None`` for an open external value."""
        value = self.error_details.get("reason")
        return value if is_tool_diagnostic_reason(value) else None


def raise_tool_diagnostic(
    *,
    message: str,
    error_kind: ToolDiagnosticKind,
    reason: ToolDiagnosticReason,
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

    Only a capability diagnostic (``ToolDiagnosticError``) and pydantic argument
    validation are expected boundary results. Bare ``ValueError``, ``OSError``,
    and every other exception remain programming/infrastructure errors and
    propagate unchanged.
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
