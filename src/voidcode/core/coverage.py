"""Model result projection and canonical tool resource paths."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..sdk.limits import TOOL_OUTPUT_BYTES
from ..sdk.tools import TextOutput, ToolFailure, ToolResult

#: Appended to a model projection that had to be cut at the model bound.
MODEL_PROJECTION_MARKER = "\n[model projection truncated; durable result unchanged]"


def model_result_text(result: ToolResult) -> tuple[str | None, bool]:
    """The exact text this result contributes to the model, and whether it had to be clipped.

    One helper for every projection of a committed result (a native tool message, a deferred
    result note), so the model bound is applied in exactly one place.
    """
    output = result.output
    text = output.text if isinstance(output, TextOutput) else output.presentation
    if result.body is not None:
        body = json.dumps(result.body.as_payload(), ensure_ascii=False, allow_nan=False)
        text = body if text is None else f"{body}\n{text}"
    if isinstance(result, ToolFailure):
        text = result.error if text is None else f"{result.error}\n{text}"
    if text is None or len(text.encode("utf-8")) <= TOOL_OUTPUT_BYTES:
        return text, False
    clipped = text.encode("utf-8")[:TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return clipped + MODEL_PROJECTION_MARKER, True


def canonical_path(workspace: Path, path: str) -> str:
    """Absolute, symlink-resolved form of ``path``, resolved against ``workspace`` when relative.

    A tool declares paths in its own notation; every reader of this module must agree on one
    spelling, so the resolution rule lives here rather than in each caller.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(workspace) / candidate
    return str(candidate.resolve())


def _target_paths(workspace: Path, arguments: Mapping[str, object] | None, path_argument_keys: Sequence[str]) -> set[str]:
    """Paths a call names through its declared argument keys (``key`` or ``key[].member``)."""
    if not arguments or not path_argument_keys:
        return set()
    targets: set[str] = set()
    for key in path_argument_keys:
        array_key, _, member_key = key.partition("[].")
        if member_key:
            items = arguments.get(array_key)
            if isinstance(items, (list, tuple)):
                for item in items:
                    value = item.get(member_key) if isinstance(item, Mapping) else None
                    if isinstance(value, str) and value:
                        targets.add(canonical_path(workspace, value))
            continue
        value = arguments.get(key)
        if isinstance(value, str) and value:
            targets.add(canonical_path(workspace, value))
    return targets
