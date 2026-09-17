"""Plain-text output shaping shared by CLI handlers."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..cli_support import EXIT_SUCCESS, print_json
from ..runtime.contracts import CapabilityStatusSnapshot


def safe_detail(value: object, *, limit: int = 160) -> str:
    text = str(value) if value is not None else ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def emit_output(
    args: object,
    payload: object,
    plain_printer: Callable[[], object],
) -> int:
    """Emit handler output as JSON (when --json) or via the plain printer."""
    if getattr(args, "json", False):
        print_json(payload)
    else:
        plain_printer()
    return EXIT_SUCCESS


def print_runtime_output(output: str | None) -> None:
    print("RESULT", flush=True)
    print(output or "", end="", flush=True)
    if output and not output.endswith("\n"):
        print(flush=True)


def print_plain_runtime_output(output: str | None) -> None:
    if output is None:
        return
    print(output, end="", flush=True)
    if not output.endswith("\n"):
        print(flush=True)


def format_named_record(prefix: str, fields: Sequence[tuple[str, object]]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in fields)
    return f"{prefix} {suffix}" if suffix else prefix


def format_rate(value: object) -> str:
    if not isinstance(value, int | float):
        return "-"
    return f"{value * 100:.1f}%"


def mcp_status_payload(snapshot: CapabilityStatusSnapshot) -> dict[str, object]:
    state = snapshot.state
    error = snapshot.error
    details = snapshot.details
    return {
        "state": state,
        "error": error,
        "details": details,
    }
