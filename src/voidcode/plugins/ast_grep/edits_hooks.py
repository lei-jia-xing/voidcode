"""Post-edit boundaries for the workspace edit tools.

The ported edit tools ran the legacy formatter executor, read hook config to
decide formatting/skip rules, and attached post-edit LSP diagnostics.
Formatting now runs as the formatter plugin's post-tool hook, outside this call;
these probes look for optional context callbacks (``format_file``,
``post_edit_diagnostics``) that the current ``ToolContext`` does not supply, so
the inline formatter/diagnostic sections stay empty.

ponytail: one swap point for formatter + post-edit LSP diagnostics wiring.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def format_after_write(*, context: Any, path: Path, previous: str | None, content: str) -> tuple[dict[str, object], tuple[Mapping[str, object], ...]]:
    """Format one written file and report its diagnostics.

    Returns the formatter payload and diagnostics the edit result carries. With
    no formatter service available the payload is empty and no diagnostics are
    produced; the write itself is unaffected.
    """
    runner = getattr(context, "format_file", None)
    if runner is None:
        return {}, ()
    result = runner(path=path, previous=previous, content=content)
    if not isinstance(result, tuple) or len(result) != 2:
        return {}, ()
    payload, diagnostics = result
    return (dict(payload) if isinstance(payload, Mapping) else {}), tuple(diagnostics or ())


def post_edit_diagnostics(*, context: Any, path: Path, content: str) -> Mapping[str, object] | None:
    """Attach post-edit LSP diagnostics when a diagnostics service is available."""
    requester = getattr(context, "post_edit_diagnostics", None)
    if requester is None:
        return None
    result = requester(path=path, content=content)
    return result if isinstance(result, Mapping) else None
