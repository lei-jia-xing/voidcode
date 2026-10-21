"""Post-edit boundary for the workspace edit tools.

The ported edit tools attached post-edit LSP diagnostics here. The probe looks
for an optional context callback (``post_edit_diagnostics``) that the current
``ToolContext`` does not supply, so the inline diagnostic section stays empty.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def post_edit_diagnostics(*, context: Any, path: Path, content: str) -> Mapping[str, object] | None:
    """Attach post-edit LSP diagnostics when a diagnostics service is available."""
    requester = getattr(context, "post_edit_diagnostics", None)
    if requester is None:
        return None
    result = requester(path=path, content=content)
    return result if isinstance(result, Mapping) else None
