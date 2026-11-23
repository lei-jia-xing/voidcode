"""Bounded model projections, distinct from immutable complete tool results."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import math
import re
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..sdk.codec import decode_provider_result
from ..sdk.hooks import RequestWindow
from ..sdk.provider import ImageAttachment, ModelContextLimits
from ..sdk.services import ArtifactPage, ArtifactReference
from ..sdk.tools import (
    RASTER_IMAGE_MIMES,
    AttachmentOutput,
    CompositeOutput,
    EmptyOutput,
    JsonPart,
    MediaPart,
    TablePart,
    TextOutput,
    TextPart,
    ToolFailure,
    ToolResult,
    composite_ordered_text,
)
from .artifacts import MAX_PAGE_BYTES
from .events import EventType

MODEL_PROJECTION_MARKER = "partial model projection; complete result unchanged"
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


def _escaped_prefix_bytes(value: str) -> list[int]:
    """Cumulative escaped UTF-8 bytes per character prefix, from one serialize.

    Walks the single ``json.dumps`` of the whole value, so per-prefix lengths never
    re-serialize: backslash units (including defensive ``\\uXXXX`` pairs) and raw
    characters (including multibyte) each advance exactly one source character.
    """
    content = json.dumps(value, ensure_ascii=False)[1:-1]
    bounds = [0]
    pos = 0
    for _ in range(len(value)):
        end = pos + 1
        if content[pos] == "\\":
            end = pos + 2
            if content[pos + 1] == "u":
                end = pos + 6
                if content[pos + 6 : pos + 8] == "\\u" and "\ud800" <= chr(int(content[pos + 2 : pos + 6], 16)) <= "\udbff":
                    end = pos + 12
        bounds.append(bounds[-1] + len(content[pos:end].encode("utf-8")))
        pos = end
    return bounds


def _fit_field(document: dict[str, object], name: str, value: str, budget: int) -> bool:
    """Keep valid JSON and complete Unicode code points; return whether the field was shortened."""
    overhead = len(_json({**document, name: ""}).encode("utf-8"))
    bounds = _escaped_prefix_bytes(value)
    room = budget - overhead
    if room < 0:
        # Even the empty field exceeds the budget: bottom out like the old binary
        # search; the caller re-checks fit and raises.
        document[name] = ""
        return True
    length = bisect_right(bounds, room) - 1
    document[name] = value[:length]
    shortened = length < len(value)
    if not _fits(document, budget):
        raise AssertionError("prefix budget arithmetic must fit")
    if shortened and bounds[length + 1] <= room:
        raise AssertionError("prefix budget arithmetic must be maximal")
    return shortened


def _textual_artifact(reference: ArtifactReference) -> bool:
    mime = reference.media_type.split(";", 1)[0].strip().lower()
    return (
        mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/javascript", "application/x-javascript"}
        or mime.endswith(("+json", "+xml"))
    )


def _composite_part_meta(part: TextPart | MediaPart | TablePart | JsonPart) -> dict[str, object]:
    """One part's complete-data identity; no base64, no row matrices."""
    if isinstance(part, TextPart):
        meta: dict[str, object] = {"kind": "text"}
        if part.reference is not None:
            meta["uri"] = part.reference.uri
        return meta
    if isinstance(part, MediaPart):
        return {"kind": "media", "media_type": part.mime, "uri": part.reference.uri}
    if isinstance(part, TablePart):
        meta = {"kind": "table", "shown": len(part.rows), "total": part.total_rows if part.total_rows is not None else len(part.rows)}
        if part.reference is not None:
            meta["uri"] = part.reference.uri
        return meta
    return {"kind": "json", "media_type": part.media_type, "uri": part.reference.uri}


def _composite_uris(output: CompositeOutput) -> list[str]:
    """Ordered unique complete-data URIs: top-level first, then parts in order."""
    uris: list[str] = []
    seen: set[str] = set()

    def _add(uri: str) -> None:
        if uri not in seen:
            seen.add(uri)
            uris.append(uri)

    top = output.bounds.reference
    if isinstance(top, ArtifactReference):
        _add(top.uri)
    elif isinstance(top, str) and top:
        _add(top)
    for part in output.parts:
        reference = part.reference
        if reference is not None:
            _add(reference.uri)
    return uris


def project_tool_result(
    result: ToolResult,
    *,
    max_bytes: int,
    artifact_reader: ArtifactPageReader | None = None,
    include_images: bool = True,
) -> tuple[str, tuple[ImageAttachment, ...], bool]:
    """Project a tool result into model text, image attachments, and a clipped indicator."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("model projection budget must be a positive integer")
    output = result.output
    document: dict[str, object] = {"status": result.status, "tool_name": result.tool_name}
    if result.result_reference is not None:
        document["result_uri"] = result.result_reference.uri
    reference = output.bounds.reference
    if reference is not None:
        document["output_uri"] = reference.uri if isinstance(reference, ArtifactReference) else reference
    if isinstance(output, CompositeOutput):
        uris = _composite_uris(output)
        if uris:
            document["output_uris"] = uris
        document["parts"] = [_composite_part_meta(part) for part in output.parts]
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
    if isinstance(output, CompositeOutput):
        if output.presentation:
            document["presentation"] = output.presentation
        ordered = composite_ordered_text(output)
        projected_text = ordered if ordered else None
    else:
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
        if isinstance(output, (TextOutput, CompositeOutput)):
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
    if include_images and isinstance(output, CompositeOutput):
        raster = [part for part in output.parts if isinstance(part, MediaPart) and part.mime in RASTER_IMAGE_MIMES]
        if raster:
            if artifact_reader is None:
                raise ValueError("reference-backed image projection requires an artifact page reader")
            decoded: list[ImageAttachment] = []
            for part in raster:
                content = read_artifact_content(part.reference, artifact_reader)
                decoded.append(ImageAttachment(part.mime, base64.b64encode(content).decode("ascii"), part.reference.sha256))
            images = tuple(decoded)
    return _json(document), images, clipped


class CommittedEvent(Protocol):
    """The committed-history surface the allocator pairs rates from (reopen-safe)."""

    @property
    def sequence(self) -> int: ...
    @property
    def event_type(self) -> str: ...
    @property
    def payload(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ProjectionAllocation:
    """One dispatch's tool-text sizing decision: fixed host policy or adaptive shrink.

    ``per_tool_max_bytes`` is set only for ``adaptive`` and never exceeds the
    host ceiling the caller passed in. ``fixed`` keeps current behavior exactly;
    ``reason`` names why (``unknown_limits``, ``unknown_usage``,
    ``images_present``, ``no_shrinkable``, ``within_ceiling``,
    ``insufficient_budget``) or ``ok`` for adaptive.
    """

    policy: Literal["fixed", "adaptive"]
    reason: str
    limit_tokens: int | None
    limit_source: str | None
    rate_tokens_per_char: float | None
    per_tool_max_bytes: int | None
    tool_count: int

    def as_payload(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "reason": self.reason,
            "limit_tokens": self.limit_tokens,
            "limit_source": self.limit_source,
            "rate_tokens_per_char": self.rate_tokens_per_char,
            "per_tool_max_bytes": self.per_tool_max_bytes,
            "tool_count": self.tool_count,
        }


def effective_input_limit_tokens(limits: ModelContextLimits | None) -> tuple[int | None, str | None]:
    """The honest input law from advertised limits, or unknown.

    ``max_input_tokens`` is the input law when advertised. Otherwise the window
    minus the advertised output maximum is usable for input — and when the
    output maximum is unadvertised too, the reserve is unknowable, so the
    answer is unknown rather than a guessed fraction of the window.
    """
    if limits is None:
        return None, None
    candidate = limits.max_input_tokens
    if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
        return candidate, "max_input_tokens"
    window = limits.context_window
    output = limits.max_output_tokens
    if (
        isinstance(window, int)
        and not isinstance(window, bool)
        and window > 0
        and isinstance(output, int)
        and not isinstance(output, bool)
        and output > 0
        and window - output >= 1
    ):
        return window - output, "context_window_minus_max_output"
    return None, None


def measured_tokens_per_character(events: Sequence[CommittedEvent], *, provider_name: str, model_name: str) -> float | None:
    """Tokens per character from this session's own newest qualifying dispatch.

    The pair is the provider's reported ``input_tokens`` on a committed
    ``MODEL_TURN`` for the same provider/model and the exact dispatched
    request's measured window. Only a zero-image dispatch qualifies: images
    are absent from the character inventory, so any image presence (or a
    pre-inventory ``None``) would pollute the rate. ``None`` means no usable
    measurement — never a byte-guess fallback.
    """
    if not isinstance(provider_name, str) or not provider_name or not isinstance(model_name, str) or not model_name:
        return None
    intent_payloads = {event.sequence: event.payload for event in events if event.event_type == EventType.PROVIDER_INTENT}
    for event in reversed(events):
        if event.event_type != EventType.MODEL_TURN:
            continue
        payload = event.payload
        if not isinstance(payload, Mapping):
            continue
        if payload.get("provider") != provider_name or payload.get("model") != model_name:
            continue
        dispatch = payload.get("dispatch_sequence")
        if type(dispatch) is not int:
            continue
        intent_payload = intent_payloads.get(dispatch)
        if not isinstance(intent_payload, Mapping):
            continue
        raw_window = intent_payload.get("request_window")
        if not isinstance(raw_window, Mapping):
            continue
        try:
            window = RequestWindow.from_payload(raw_window)
        except ValueError, KeyError, TypeError:
            continue
        if window.total_image_count != 0:
            continue
        characters = window.total_characters
        if characters <= 0:
            continue
        result = payload.get("result")
        if not isinstance(result, Mapping):
            continue
        try:
            turn = decode_provider_result(result)
        except ValueError, KeyError, TypeError:
            continue
        tokens = None if turn.usage is None else turn.usage.input_tokens
        if type(tokens) is not int or tokens <= 0:
            continue
        return tokens / characters
    return None


def allocate_tool_text_budget(
    *,
    tool_count: int,
    fixed_characters: int,
    tool_schema_characters: int,
    images_present: bool,
    limits: ModelContextLimits | None,
    rate_tokens_per_char: float | None,
    ceiling_bytes: int,
) -> ProjectionAllocation:
    """Size one dispatch's per-tool text budget: shrink-only under the host ceiling.

    ``fixed_characters`` measures everything already assembled that is not a
    shrinkable tool projection (message contents, reasoning, deferred labels).
    The byte budget for a character share is the share itself: UTF-8 text that
    fits in B bytes holds at most B characters, so the conversion is
    conservative by construction (it may under-fill multibyte text, never
    over-fill). Any unknown input keeps the fixed host policy explicitly.
    """
    if type(ceiling_bytes) is not int or ceiling_bytes < 1:
        raise ValueError("projection ceiling must be a positive integer")
    for name, value in (("tool_count", tool_count), ("fixed_characters", fixed_characters), ("tool_schema_characters", tool_schema_characters)):
        if type(value) is not int or value < 0:
            raise ValueError(f"projection allocation field {name} must be a non-negative integer")
    limit_tokens, limit_source = effective_input_limit_tokens(limits)

    def fixed(reason: str) -> ProjectionAllocation:
        return ProjectionAllocation(
            policy="fixed",
            reason=reason,
            limit_tokens=limit_tokens,
            limit_source=limit_source,
            rate_tokens_per_char=rate_tokens_per_char,
            per_tool_max_bytes=None,
            tool_count=tool_count,
        )

    if tool_count <= 0:
        return fixed("no_shrinkable")
    if images_present:
        return fixed("images_present")
    if limit_tokens is None:
        return fixed("unknown_limits")
    if (
        rate_tokens_per_char is None
        or isinstance(rate_tokens_per_char, bool)
        or not isinstance(rate_tokens_per_char, (int, float))
        or not math.isfinite(rate_tokens_per_char)
        or rate_tokens_per_char <= 0
    ):
        return fixed("unknown_usage")
    shrinkable = int(limit_tokens / rate_tokens_per_char) - fixed_characters - tool_schema_characters
    if shrinkable < tool_count:
        return fixed("insufficient_budget")
    share = shrinkable // tool_count
    if share >= ceiling_bytes:
        return fixed("within_ceiling")
    return ProjectionAllocation(
        policy="adaptive",
        reason="ok",
        limit_tokens=limit_tokens,
        limit_source=limit_source,
        rate_tokens_per_char=rate_tokens_per_char,
        per_tool_max_bytes=share,
        tool_count=tool_count,
    )


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
