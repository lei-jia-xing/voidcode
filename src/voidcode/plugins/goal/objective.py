"""Goal objective storage: inline context or immutable artifact spill."""

from __future__ import annotations

import hashlib

from voidcode.sdk.services import ArtifactServices

from .state import GoalState

#: Inline characters repeated in every goal context contribution. Larger objectives
#: use a durable URI instead, preserving the prior small-goal context footprint
#: without limiting what can be stored or loaded. This is not an ingress bound.
OBJECTIVE_INLINE_CONTEXT_CHARS = 4000


def spill_objective_if_needed(services: ArtifactServices, objective: str) -> tuple[str, str | None, str | None]:
    """Keep short context inline; store larger objectives without workspace mutations."""
    if len(objective) <= OBJECTIVE_INLINE_CONTEXT_CHARS:
        return objective, None, None

    raw_bytes = objective.encode("utf-8")
    artifact = services.spill_artifact(raw_bytes, media_type="text/markdown; charset=utf-8")
    prompt_text = f"Read the goal objective at {artifact.uri} before continuing."
    return prompt_text, artifact.uri, artifact.sha256


def read_full_objective(state: GoalState, services: ArtifactServices) -> str:
    """Read the full objective text, verifying integrity when spilled.

    Inline objectives return as-is. Spilled objectives are read from the
    content-addressed store via services.read_artifact with SHA-256 verification.
    """
    if state.objective_file_ref is None:
        return state.objective
    if not state.objective_file_ref.startswith("artifact://"):
        raise ValueError(f"Goal objective reference must be an artifact URI: {state.objective_file_ref}")
    digest = state.objective_file_ref[len("artifact://") :]
    if state.objective_file_sha256 != digest:
        raise ValueError(f"Goal objective artifact reference/hash mismatch: {state.objective_file_ref}")
    chunks: list[bytes] = []
    offset = 0
    while True:
        page = services.read_artifact(digest, offset=offset)
        chunks.append(page.data)
        if page.eof:
            break
        if not page.data:
            raise OSError(f"Goal objective artifact read made no progress: {state.objective_file_ref}")
        offset += len(page.data)
    raw_bytes = b"".join(chunks)
    if hashlib.sha256(raw_bytes).hexdigest() != digest:
        raise ValueError(f"Goal objective artifact tampered or corrupted: {state.objective_file_ref}")
    return raw_bytes.decode("utf-8")
