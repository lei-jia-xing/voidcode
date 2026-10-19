"""Read coverage: what a session's model has genuinely been shown.

Nothing here knows any product's read semantics. A read-shaped tool declares what its own result
made visible (`ReadObservation`), the kernel commits that result, and this module decides what a
later call may rely on. Coverage is derived from committed results only, never from a tool's
claim about its intent, and every uncertain case fails closed by granting nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ..sdk.codec import decode_tool_result
from ..sdk.limits import TOOL_OUTPUT_BYTES
from ..sdk.tools import TextOutput, ToolFailure, ToolResult, ToolSuccess
from .storage.store import StoredEvent

#: Appended to a model projection that had to be cut at the model bound.
MODEL_PROJECTION_MARKER = "\n[model projection truncated; durable result unchanged]"


def model_result_text(result: ToolResult) -> tuple[str | None, bool]:
    """The exact text this result contributes to the model, and whether it had to be clipped.

    One helper for every projection of a committed result (a native tool message, a deferred
    result note), so the model bound is applied in exactly one place and coverage can never
    outrun what the model was actually shown.
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


@dataclass(frozen=True, slots=True)
class ReadCoverage:
    """Coverage granted by one session's committed, model-visible results."""

    read_paths: frozenset[str] = frozenset()
    #: Lines keyed by ``(canonical path, content hash)``: two versions never merge.
    read_lines: Mapping[tuple[str, str], frozenset[int]] = field(default_factory=lambda: MappingProxyType({}))
    read_whole_files: frozenset[tuple[str, str]] = frozenset()
    #: The single content version this call targets, or ``None`` when that is ambiguous.
    read_hash: str | None = None


def read_coverage(
    events: Iterable[StoredEvent | Mapping[str, object]],
    *,
    workspace: Path,
    session_id: str,
    arguments: Mapping[str, object] | None = None,
    path_argument_keys: Sequence[str] = (),
) -> ReadCoverage:
    """Coverage this session's model has seen, rebuilt from committed results.

    ``events`` are the session's committed events (an explicit result payload is also accepted
    for a caller that holds a result but no event; such a payload carries no session identity, so
    the caller stands behind it). Only completed, model-visible results count — a deferred
    receipt's completion counts exactly like an inline one: a failure, an interrupted result, a
    clipped projection, a partial/truncated output, and an observation the tool itself marked
    clipped all grant nothing. ``read_hash`` is the unique content version observed for the paths
    ``path_argument_keys`` declare in ``arguments``, and ``None`` when no path is declared or
    several versions are in play.
    """
    workspace = Path(workspace)
    paths: set[str] = set()
    lines: dict[tuple[str, str], set[int]] = {}
    counts: dict[tuple[str, str], set[int]] = {}
    whole_claims: set[tuple[str, str]] = set()
    for event in events:
        if isinstance(event, StoredEvent):
            if event.session_id != session_id or event.event_type != "kernel.tool_completed":
                continue
            raw_result: object = event.payload.get("result")
        else:
            raw_result = event
        result = _committed_result(raw_result)
        if result is None:
            continue
        _, clipped = model_result_text(result)
        if clipped or result.output.bounds.truncated or result.output.bounds.partial:
            continue
        for observation in result.coverage:
            if observation.clipped:
                continue
            key = (canonical_path(workspace, observation.path), observation.content_hash)
            paths.add(key[0])
            lines.setdefault(key, set()).update(observation.lines)
            counts.setdefault(key, set()).add(observation.line_count)
            if observation.whole_file:
                whole_claims.add(key)
    whole_files = frozenset(key for key in whole_claims if len(counts[key]) == 1 and lines[key] == set(range(1, next(iter(counts[key])) + 1)))
    return ReadCoverage(
        read_paths=frozenset(paths),
        read_lines=MappingProxyType({key: frozenset(observed) for key, observed in lines.items()}),
        read_whole_files=whole_files,
        read_hash=_target_read_hash(workspace, arguments, path_argument_keys, lines),
    )


def _committed_result(payload: object) -> ToolSuccess | None:
    """Decode a committed result payload; anything malformed or unsuccessful grants nothing."""
    if not isinstance(payload, Mapping):
        return None
    try:
        result = decode_tool_result(payload)
    except ValueError:
        return None
    if isinstance(result, ToolFailure):
        return None
    return result


def _target_read_hash(
    workspace: Path,
    arguments: Mapping[str, object] | None,
    path_argument_keys: Sequence[str],
    observed: Mapping[tuple[str, str], set[int]],
) -> str | None:
    targets = _target_paths(workspace, arguments, path_argument_keys)
    digests = {digest for path, digest in observed if path in targets}
    return next(iter(digests)) if len(digests) == 1 else None


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
