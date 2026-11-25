"""Bounded output pages and structured delivery reads for child outcomes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from voidcode.capabilities.resource_pages import render_stored_artifact_page
from voidcode.sdk.child_schema import validate_child_text
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourceReadCaller
from voidcode.sdk.services import ArtifactServices, ChildOutcome, StructuredQueryParseError
from voidcode.sdk.tools import TextOutput

from .constants import _STRUCTURED_READ_PAGE_BYTES, MAX_STRUCTURED_OUTPUT_BYTES
from .uris import _artifact_uri


def _default_structured_output_cap() -> int:
    return MAX_STRUCTURED_OUTPUT_BYTES


_structured_output_cap: Callable[[], int] = _default_structured_output_cap


def _configure_structured_output_cap(provider: Callable[[], int]) -> None:
    """Point the validation read at the facade's live byte cap.

    The facade owns the documented cap and remains the historical monkeypatch surface;
    the provider keeps that attribute authoritative without a circular import.
    """
    global _structured_output_cap
    _structured_output_cap = provider


@dataclass(frozen=True, slots=True)
class _OutputFacts:
    """One bounded output page plus the durable references that reach the rest."""

    output: str | None = None
    output_uri: str | None = None
    output_offset: int | None = None
    output_bytes: int | None = None
    next_output_offset: int | None = None
    output_preview: str | None = None


def _resource_caller(context: ToolContext) -> ResourceReadCaller:
    return ResourceReadCaller(
        session_id=context.require_session_id(),
        workspace=context.workspace,
        revision_id=context.revision_id,
        run_id=context.run_id,
        abort_signal=context.abort_signal,
    )


def _artifact_output_facts(
    services: ArtifactServices,
    caller: ResourceReadCaller,
    digest: str,
    offset: int,
    limit: int,
) -> _OutputFacts:
    uri = _artifact_uri(digest)
    page = render_stored_artifact_page(
        uri=uri,
        digest=digest,
        services=services,
        caller=caller,
        byte_offset=offset,
        # Always make room for a complete UTF-8 scalar, even when a caller
        # asks for a smaller page. The returned cursor counts decoded bytes.
        byte_limit=max(limit, 4),
    )
    if page is None:
        raise RuntimeError("committed child output artifact is missing")
    if not isinstance(page.output, TextOutput):
        raise RuntimeError("committed child output artifact is not text")
    artifact = page.body.get("artifact")
    raw_content = page.body.get("raw_content")
    next_offset = page.body.get("next_byte_offset")
    total_bytes = artifact.get("bytes") if isinstance(artifact, Mapping) else None
    if type(total_bytes) is not int or not isinstance(raw_content, str):
        raise RuntimeError("committed child output artifact page is malformed")
    if next_offset is not None and (type(next_offset) is not int or next_offset <= offset):
        raise RuntimeError("committed child output artifact page did not advance")
    return _OutputFacts(
        output=raw_content,
        output_uri=uri,
        output_offset=offset,
        output_bytes=total_bytes,
        next_output_offset=next_offset,
    )


def _output_facts(
    outcome: ChildOutcome,
    services: ArtifactServices,
    caller: ResourceReadCaller,
    offset: int,
    limit: int,
) -> _OutputFacts:
    """The bounded page plus its durable references; never a megabyte of inline output."""
    if outcome.output_sha256 is not None:
        return _artifact_output_facts(services, caller, outcome.output_sha256, offset, limit)
    if outcome.output is not None:
        raw = outcome.output.encode("utf-8")
        if offset > 0 or len(raw) > limit:
            reference = services.spill_artifact(raw, media_type="text/plain; charset=utf-8")
            return _artifact_output_facts(services, caller, reference.sha256, offset, limit)
        return _OutputFacts(
            output=outcome.output,
            output_offset=0,
            output_bytes=len(raw),
        )
    return _OutputFacts(
        output_preview=outcome.output_preview,
        output_bytes=outcome.output_bytes,
    )


@dataclass(frozen=True, slots=True)
class _StructuredFacts:
    """Consumer-side structured reading of one child run.

    Exactly one of ``data``/``parse_error`` is set when a declared schema was
    validated against full captured text. All-``None`` means either undeclared
    (the caller held no schema, so no reading was attempted) or undelivered
    (a schema is declared but the run is not completed, so status governs).
    ``unavailable`` is set when a schema was declared and the run completed, but
    no full text was captured or the text exceeded the structured-read byte cap;
    it carries the committed capture cause or the measured oversize cause.
    """

    data: dict[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None
    unavailable: str | None = None


@dataclass(frozen=True, slots=True)
class _FullOutput:
    """Full terminal text or the typed reason it was not read."""

    text: str | None = None
    error: str | None = None


def _full_output_text(services: ArtifactServices, outcome: ChildOutcome) -> _FullOutput:
    """Full terminal text for structured validation, bounded by the facade's output cap.

    Artifact-backed output is joined as bytes and decoded once as strict UTF-8; inline
    output is already whole. Preview bytes are never returned, and an artifact over the
    cap is refused before the read can grow without bound.
    """
    if outcome.output_sha256 is not None:
        cap = _structured_output_cap()
        chunks: list[bytes] = []
        offset = 0
        while True:
            page = services.read_artifact(outcome.output_sha256, offset=offset, limit=_STRUCTURED_READ_PAGE_BYTES)
            if not page.data and not page.eof:
                return _FullOutput(error="structured validation failed: artifact read made no progress")
            if offset + len(page.data) > cap:
                return _FullOutput(error=(f"structured validation skipped: full output is {page.artifact.bytes} bytes, above the {cap}-byte cap"))
            chunks.append(bytes(page.data))
            if page.eof:
                break
            offset += len(page.data)
        return _FullOutput(text=b"".join(chunks).decode("utf-8"))
    return _FullOutput(text=outcome.output)


def _structured_facts(
    result_schema: Mapping[str, object] | None,
    outcome: ChildOutcome,
    services: ArtifactServices,
) -> _StructuredFacts:
    """Validate one completed run's full text; every degraded path stays explicit."""
    if result_schema is None:
        return _StructuredFacts()
    if outcome.status != "completed":
        return _StructuredFacts()
    full = _full_output_text(services, outcome)
    if full.error is not None:
        return _StructuredFacts(unavailable=full.error)
    if full.text is None:
        return _StructuredFacts(unavailable=outcome.output_error or "no full output was captured")
    data, error = validate_child_text(result_schema, full.text)
    if error is not None:
        return _StructuredFacts(parse_error=error)
    assert data is not None
    return _StructuredFacts(data=data)
