"""Unit tests for artifact URL reads wired to the core artifact store."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.plugins.files import create_plugin
from voidcode.plugins.files.read_tool import MAX_ARTIFACT_PAGE_BYTES, MAX_INLINE_ATTACHMENT_BYTES
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.sdk.tools import AttachmentOutput, OpaqueToolBody, TextOutput, ToolCall, ToolSuccess


class FakeArtifactStore:
    """Minimal stand-in for the core store: content-addressed bytes, bounded pages."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.media_types: dict[str, str] = {}
        self.spills: list[str] = []

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        sha256 = hashlib.sha256(raw).hexdigest()
        self.blobs[sha256] = raw
        self.media_types[sha256] = media_type
        self.spills.append(media_type)
        return ArtifactReference(sha256=sha256, bytes=len(raw), media_type=media_type)

    def read_artifact(self, reference: object, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        sha256 = reference.sha256 if isinstance(reference, ArtifactReference) else str(reference)
        raw = self.blobs[sha256]
        start = min(max(0, offset), len(raw))
        page = raw[start : start + limit]
        return ArtifactPage(
            sha256=sha256,
            offset=start,
            total_bytes=len(raw),
            data=page,
            eof=start + len(page) >= len(raw),
            media_type=self.media_types.get(sha256, ""),
        )


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        session_id="session-artifacts",
        run_id="run-artifacts",
        invocation_id="inv-artifacts",
        revision_id="rev-artifacts",
        actor="model",
    )


def _read_success(store: FakeArtifactStore, tmp_path: Path, **arguments: object) -> ToolSuccess:
    tool = next(tool for tool in create_plugin({}).tools if tool.definition.name == "read")
    result = tool.invoke(ToolCall(tool_name="read", arguments=arguments), context=_context(tmp_path), services=cast(Any, store))
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, OpaqueToolBody)
    return result


def _read_body(store: FakeArtifactStore, tmp_path: Path, **arguments: object) -> tuple[dict[str, object], str]:
    result = _read_success(store, tmp_path, **arguments)
    assert isinstance(result.output, TextOutput)
    return result.body.as_payload(), result.output.text


def _read_attachment(store: FakeArtifactStore, tmp_path: Path, **arguments: object) -> tuple[dict[str, object], AttachmentOutput]:
    result = _read_success(store, tmp_path, **arguments)
    assert isinstance(result.output, AttachmentOutput)
    return result.body.as_payload(), result.output


def test_spill_then_read_line_page_round_trips(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    text = "".join(f"artifact-line-{index}\n" for index in range(1, 6))
    artifact = store.spill_artifact(text, media_type="text/plain; charset=utf-8")
    sha256 = artifact.sha256
    assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    uri = f"voidcode://artifact/{sha256}"

    body, output = _read_body(store, tmp_path, path=uri, offset=2, limit=2)
    assert body["offset"] == 2
    assert body["offset_unit"] == "lines"
    assert body["limit"] == 2
    assert body["line_count"] == 2
    assert body["raw_content"] == "artifact-line-2\nartifact-line-3\n"
    assert body["content_hash"] == sha256
    assert body["artifact_byte_count"] == len(text.encode("utf-8"))
    assert body["next_offset"] == 4
    assert body["truncated"] is True
    assert "artifact-line-2" in output

    tail, _ = _read_body(store, tmp_path, path=uri, offset=4, limit=2)
    assert tail["raw_content"] == "artifact-line-4\nartifact-line-5\n"
    assert tail["line_count"] == 2
    assert tail["next_offset"] is None
    assert tail["truncated"] is False


def test_artifact_byte_page_honors_byte_window(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    artifact = store.spill_artifact(b"0123456789abcdef", media_type="application/octet-stream")
    uri = f"voidcode://artifact/{artifact.sha256}"

    body, _ = _read_body(store, tmp_path, path=uri, byte_offset=4, byte_limit=4)
    assert body["offset_unit"] == "bytes"
    assert body["offset"] == 4
    assert body["byte_limit"] == 4
    assert body["raw_content"] == "4567"
    assert body["byte_count"] == 4
    assert body["artifact_byte_count"] == 16
    assert body["next_byte_offset"] == 8
    assert body["truncated"] is True
    assert body["content_hash"] == artifact.sha256

    tail, _ = _read_body(store, tmp_path, path=uri, byte_offset=8, byte_limit=8)
    assert tail["raw_content"] == "89abcdef"
    assert tail["next_byte_offset"] is None
    assert tail["truncated"] is False


def test_unknown_artifact_id_is_refused(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    with pytest.raises(ValueError, match="cannot resolve voidcode://artifact URL"):
        _read_body(store, tmp_path, path=f"voidcode://artifact/{'0' * 64}")


def test_invalid_artifact_id_is_refused(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    with pytest.raises(ValueError, match="invalid artifact id: not-a-sha256"):
        _read_body(store, tmp_path, path="voidcode://artifact/not-a-sha256")
    with pytest.raises(ValueError, match="requires an artifact id"):
        _read_body(store, tmp_path, path="voidcode://artifact/")


def test_pdf_magic_is_required_for_the_pdf_view(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    artifact = store.spill_artifact(b"plain text, not a PDF\n", media_type="application/octet-stream")
    with pytest.raises(ValueError, match="PDF artifact view requires a readable PDF artifact"):
        _read_body(store, tmp_path, path=f"voidcode://artifact/{artifact.sha256}", view="pdf")


def _simple_pdf(text: str) -> bytes:
    """A minimal single-page PDF whose text layer `pdftotext` extracts."""

    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def test_image_artifact_renders_as_attachment(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    artifact = store.spill_artifact(png, media_type="image/png")
    sha256 = artifact.sha256
    uri = f"voidcode://artifact/{sha256}"

    body, output = _read_attachment(store, tmp_path, path=uri)

    assert output.mime == "image/png"
    assert output.data_uri == f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
    assert output.bounds.reference is not None
    assert output.bounds.reference.uri == uri
    assert output.bounds.truncated is False
    assert body["type"] == "attachment"
    assert body["path"] == uri
    assert body["artifact_id"] == sha256
    assert body["sha256"] == sha256
    assert body["content_hash"] == sha256
    assert body["byte_count"] == len(png)
    assert body["mime"] == "image/png"
    assert body["content_type"] == "image/png"
    assert body["uri"] == uri
    assert body["attachment"] == {"mime": "image/png", "reference": uri}
    assert body["bounds"] == {"reference": uri}
    # Inline bytes need no second write: the artifact is its own reference.
    assert store.spills == ["image/png"]


def test_large_image_artifact_spills_and_references(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    # Above the inline bound and across two store pages.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_INLINE_ATTACHMENT_BYTES + MAX_ARTIFACT_PAGE_BYTES)
    artifact = store.spill_artifact(png, media_type="image/png")
    sha256 = artifact.sha256
    uri = f"voidcode://artifact/{sha256}"

    body, output = _read_attachment(store, tmp_path, path=uri)

    assert output.data_uri is None
    assert uri in (output.presentation or "")
    assert output.bounds.reference is not None
    assert output.bounds.reference.uri == uri
    assert output.bounds.truncated is False
    assert output.bounds.partial is False
    assert body["type"] == "attachment"
    assert body["byte_count"] == len(png)
    assert body["artifact_id"] == sha256
    assert body["uri"] == uri
    assert body["attachment"] == {"mime": "image/png", "reference": uri}
    # The spill of already-stored bytes is content-addressed back to this artifact.
    assert store.spills == ["image/png", "image/png"]


def test_pdf_artifact_view_uses_the_media_type_claim(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    pdf = _simple_pdf("artifact-pdf-text")
    artifact = store.spill_artifact(pdf, media_type="application/pdf")
    sha256 = artifact.sha256
    uri = f"voidcode://artifact/{sha256}"

    try:
        body, output = _read_body(store, tmp_path, path=uri)
    except ValueError as exc:
        if "Poppler" in str(exc):
            pytest.skip("pdftotext is not installed")
        raise

    assert body["type"] == "pdf"
    assert body["path"] == uri
    assert body["source_reference"] == uri
    assert body["source_artifact"] == {"artifact_id": sha256, "sha256": sha256, "byte_count": len(pdf), "uri": uri}
    assert body["raw_content"].strip() == "artifact-pdf-text"
    assert output.startswith(f"Extracted PDF text from {uri}")


def test_pdf_magic_fallback_only_applies_to_an_empty_claim(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    pdf = _simple_pdf("fallback-pdf-text")
    unclaimed = store.spill_artifact(pdf, media_type="")
    # One artifact carries one claim: different bytes for the claimed variant.
    claimed_binary = store.spill_artifact(pdf + b"% claimed as binary\n", media_type="application/octet-stream")

    try:
        body, _ = _read_body(store, tmp_path, path=f"voidcode://artifact/{unclaimed.sha256}")
    except ValueError as exc:
        if "Poppler" in str(exc):
            pytest.skip("pdftotext is not installed")
        raise
    assert body["type"] == "pdf"

    # A non-empty claim wins over the magic, and image bytes without a claim stay text.
    page, _ = _read_body(store, tmp_path, path=f"voidcode://artifact/{claimed_binary.sha256}")
    assert page["type"] == "artifact"
    assert page["offset_unit"] == "lines"
    unclaimed_png = store.spill_artifact(b"\x89PNG\r\n\x1a\nplain\n", media_type="")
    image_page, _ = _read_body(store, tmp_path, path=f"voidcode://artifact/{unclaimed_png.sha256}")
    assert image_page["type"] == "artifact"
    assert image_page["offset_unit"] == "lines"


def test_text_artifact_still_pages_as_text(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    artifact = store.spill_artifact("first\nsecond\nthird\n", media_type="text/plain")
    uri = f"voidcode://artifact/{artifact.sha256}"

    body, output = _read_body(store, tmp_path, path=uri, offset=2, limit=1)

    assert body["type"] == "artifact"
    assert body["offset_unit"] == "lines"
    assert body["line_count"] == 1
    assert body["raw_content"] == "second\n"
    assert body["next_offset"] == 3
    assert body["truncated"] is True
    assert "second" in output
