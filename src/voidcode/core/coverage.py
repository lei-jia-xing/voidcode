"""Bounded model projections, distinct from immutable complete tool results."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..sdk.provider import ImageAttachment
from ..sdk.services import ArtifactPage, ArtifactReference
from ..sdk.tools import AttachmentOutput, EmptyOutput, TextOutput, ToolFailure, ToolResult
from .artifacts import MAX_PAGE_BYTES

MODEL_PROJECTION_MARKER = "partial model projection; complete result unchanged"
RASTER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
type ArtifactPageReader = Callable[[str, int, int], ArtifactPage]


class ProjectionRequiresReference(ValueError):
    """The host must persist the complete result before omitting any of its data."""


def _digest(reference: ArtifactReference | str) -> str | None:
    if isinstance(reference, ArtifactReference):
        return reference.sha256
    candidate = reference.removeprefix("artifact://")
    return candidate if _SHA256_HEX.fullmatch(candidate) is not None else None


def _read_page(reference: ArtifactReference | str, reader: ArtifactPageReader, offset: int, limit: int) -> ArtifactPage:
    digest = _digest(reference)
    if digest is None:
        raise ValueError("artifact projection requires a content-addressed reference")
    page = reader(digest, offset, limit)
    if page.artifact.sha256 != digest or page.offset != offset or len(page.data) > limit:
        raise ValueError("artifact reader returned a mismatched page")
    if isinstance(reference, ArtifactReference) and page.artifact.bytes != reference.bytes:
        raise ValueError("artifact reader changed the declared artifact size")
    end = offset + len(page.data)
    if page.artifact.bytes < 0 or end > page.artifact.bytes or page.eof != (end == page.artifact.bytes):
        raise ValueError("artifact reader returned inconsistent size or EOF")
    if not page.eof and not page.data:
        raise ValueError("artifact reader made no progress")
    return page


def read_artifact_content(reference: ArtifactReference, reader: ArtifactPageReader) -> bytes:
    """Read and verify a complete immutable object through bounded pages, never a backing path."""
    content = bytearray()
    digest = hashlib.sha256()
    while True:
        page = _read_page(reference, reader, len(content), MAX_PAGE_BYTES)
        content.extend(page.data)
        digest.update(page.data)
        if page.eof:
            break
    if digest.hexdigest() != reference.sha256:
        raise ValueError("artifact content does not match its digest")
    return bytes(content)


def _text_prefix(reference: ArtifactReference, reader: ArtifactPageReader, budget: int) -> tuple[str, bool]:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    chunks: list[str] = []
    offset = 0
    if reference.bytes == 0:
        _read_page(reference, reader, 0, 1)
        return "", False
    eof = False
    while offset < budget and not eof:
        page = _read_page(reference, reader, offset, min(MAX_PAGE_BYTES, budget - offset))
        chunks.append(decoder.decode(page.data, final=page.eof))
        offset += len(page.data)
        eof = page.eof
    return "".join(chunks), not eof


def _json(document: Mapping[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _fits(document: Mapping[str, object], budget: int) -> bool:
    return len(_json(document).encode("utf-8")) <= budget


def _fit_field(document: dict[str, object], name: str, value: str, budget: int) -> bool:
    """Keep valid JSON and complete Unicode code points; return whether the field was shortened."""
    document[name] = value
    if _fits(document, budget):
        return False
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        document[name] = value[:middle]
        if _fits(document, budget):
            low = middle
        else:
            high = middle - 1
    document[name] = value[:low]
    return True


def _textual_artifact(reference: ArtifactReference) -> bool:
    mime = reference.media_type.split(";", 1)[0].strip().lower()
    return (
        mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/javascript", "application/x-javascript"}
        or mime.endswith(("+json", "+xml"))
    )


def project_tool_result(
    result: ToolResult,
    *,
    max_bytes: int,
    artifact_reader: ArtifactPageReader | None = None,
    include_images: bool = True,
) -> tuple[str, tuple[ImageAttachment, ...], bool]:
    """Project one authoritative model representation, preserving complete-resource identities.

    ``TextOutput.text`` is the selected page; its machine body is recoverable through the
    canonical result URI, never duplicated into the model allocation. ``EmptyOutput`` bodies
    remain inline for structured-only results. Only explicit ``EmptyOutput`` references with
    textual MIME types (``text/*``, JSON/XML/JavaScript, ``+json``/``+xml``) are paged; images are read
    to EOF at dispatch. Any omission requires ``result_reference``. The final boolean means
    selected content was clipped, not that machine metadata was omitted.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("model projection budget must be a positive integer")
    output = result.output
    document: dict[str, object] = {"status": result.status, "tool_name": result.tool_name}
    if result.result_reference is not None:
        document["result_uri"] = result.result_reference.uri
    reference = output.bounds.reference
    if reference is not None:
        document["output_uri"] = reference.uri if isinstance(reference, ArtifactReference) else reference
    if output.bounds.source is not None:
        document["source"] = output.bounds.source
    if output.bounds.partial:
        document["partial"] = True
    if output.bounds.truncated:
        document["capture_truncated"] = True
    if isinstance(result, ToolFailure):
        document["error"] = result.error
        if result.diagnostics is not None:
            document["diagnostics"] = result.diagnostics.as_payload()
    out_of_line_text = isinstance(output, EmptyOutput) and isinstance(reference, ArtifactReference) and _textual_artifact(reference)
    output_text = output.text if isinstance(output, TextOutput) else output.presentation
    if out_of_line_text and output_text:
        document["presentation"] = output_text
        projected_text = None
    else:
        projected_text = output_text
    omitted = False
    clipped = False

    def partial(*, content_clipped: bool = False) -> None:
        nonlocal omitted, clipped
        omitted = True
        clipped |= content_clipped
        document["projection_note"] = MODEL_PROJECTION_MARKER

    if result.body is not None:
        if isinstance(output, TextOutput):
            document["body_omitted"] = True
            partial()
        else:
            document["body"] = result.body.as_payload()
    if not _fits(document, max_bytes):
        partial()
        for field in ("body", "diagnostics", "source", "presentation"):
            if field in document and not _fits(document, max_bytes):
                del document[field]
                document[f"{field}_omitted"] = True
                if field == "body" and isinstance(output, EmptyOutput) and not out_of_line_text:
                    partial(content_clipped=True)
        if isinstance(result, ToolFailure) and not _fits(document, max_bytes):
            document["error_partial"] = True
            if projected_text and _fits({**document, "error": "", "text": projected_text}, max_bytes):
                document["text"] = projected_text
            elif projected_text or out_of_line_text:
                partial(content_clipped=True)
                document["text_partial"] = True
            _fit_field(document, "error", result.error, max_bytes)
    if not _fits(document, max_bytes):
        raise ValueError("model projection allocation cannot fit required status and resource identities")

    text_reference = reference if out_of_line_text else None
    remaining = max(0, max_bytes - len(_json(document).encode("utf-8")))
    if text_reference is not None and text_reference.bytes > remaining:
        partial(content_clipped=True)
        document["text_partial"] = True

    text = None if out_of_line_text else projected_text
    source_clipped = False
    if text_reference is not None:
        if artifact_reader is None:
            raise ValueError("reference-backed text projection requires an artifact page reader")
        available = max(0, max_bytes - len(_json(document).encode("utf-8")))
        text, source_clipped = _text_prefix(text_reference, artifact_reader, available)
    if text:
        candidate = {**document, "text": text}
        if source_clipped or not _fits(candidate, max_bytes):
            partial(content_clipped=True)
            document["text_partial"] = True
            if _fits({**document, "text": ""}, max_bytes):
                _fit_field(document, "text", text, max_bytes)
        else:
            document["text"] = text
    elif source_clipped:
        partial(content_clipped=True)
        document["text_partial"] = True
    if omitted and result.result_reference is None:
        raise ProjectionRequiresReference("complete tool result must be persisted before projecting omitted data")
    if not _fits(document, max_bytes):
        raise ValueError("model projection allocation cannot fit required metadata")

    images: tuple[ImageAttachment, ...] = ()
    if include_images and isinstance(output, AttachmentOutput) and output.mime in RASTER_IMAGE_MIMES:
        if isinstance(reference, ArtifactReference):
            if artifact_reader is None:
                raise ValueError("reference-backed image projection requires an artifact page reader")
            content = read_artifact_content(reference, artifact_reader)
            images = (ImageAttachment(output.mime, base64.b64encode(content).decode("ascii"), reference.sha256),)
        elif output.data_uri is not None:
            prefix, separator, data = output.data_uri.partition(",")
            if not separator or prefix != f"data:{output.mime};base64":
                raise ValueError("inline image attachment has an invalid data URI")
            base64.b64decode(data, validate=True)
            images = (ImageAttachment(output.mime, data),)
        else:
            raise ValueError("image attachment has no readable original")
    return _json(document), images, clipped


def canonical_path(workspace: Path, path: str) -> str:
    """Preserve a resource URI; resolve a filesystem path absolutely against ``workspace``.

    A tool declares paths in its own notation; every reader of this module must agree on one
    spelling, so the resolution rule lives here rather than in each caller.
    """
    if "://" in path:
        return path
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
