"""Unit tests for artifact URL reads wired to the core artifact store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

from tests.fixtures.png import rgb_png
from voidcode.plugins.files import create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.sdk.tools import AttachmentOutput, OpaqueToolBody, TextOutput, ToolCall, ToolFailure, ToolSuccess

_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


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

    def spill_artifact_stream(self, chunks, *, media_type="application/octet-stream"):
        return self.spill_artifact(b"".join(chunks), media_type=media_type)

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        try:
            raw = self.blobs[sha256]
        except KeyError:
            raise FileNotFoundError(f"artifact is not stored: {sha256}") from None
        start = min(max(0, offset), len(raw))
        page = raw[start : start + limit]
        return ArtifactPage(
            artifact=ArtifactReference(sha256, len(raw), self.media_types.get(sha256, "")),
            offset=start,
            data=page,
            eof=start + len(page) >= len(raw),
        )

    def read_resource(self, request):  # type: ignore[no-untyped-def]
        from voidcode.capabilities.files.resource_readers import read_artifact_resource
        from voidcode.sdk.resources import ResourceReadCaller

        assert request.scheme == "artifact"
        caller = ResourceReadCaller(session_id="session-artifacts", workspace=Path("."), revision_id="rev-test", run_id="run-test")
        return read_artifact_resource(request, caller, self)


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


def _read_failure(store: FakeArtifactStore, tmp_path: Path, **arguments: object) -> ToolFailure:
    tool = next(tool for tool in create_plugin({}).tools if tool.definition.name == "read")
    result = tool.invoke(ToolCall(tool_name="read", arguments=arguments), context=_context(tmp_path), services=cast(Any, store))
    assert isinstance(result, ToolFailure)
    return result


def test_spill_then_read_line_page_round_trips(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    text = "".join(f"artifact-line-{index}\n" for index in range(1, 6))
    artifact = store.spill_artifact(text, media_type="text/plain; charset=utf-8")
    sha256 = artifact.sha256
    assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    uri = f"artifact://{sha256}"

    body, output = _read_body(store, tmp_path, path=uri, offset=2, limit=2)
    assert body["offset"] == 2
    assert body["offset_unit"] == "lines"
    assert body["limit"] == 2
    assert body["line_count"] == 2
    assert body["raw_content"] == "artifact-line-2\nartifact-line-3\n"
    assert body["artifact"]["sha256"] == sha256
    assert body["artifact"]["bytes"] == len(text.encode("utf-8"))
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
    uri = f"artifact://{artifact.sha256}"

    body, _ = _read_body(store, tmp_path, path=uri, byte_offset=4, byte_limit=4)
    assert body["offset_unit"] == "bytes"
    assert body["offset"] == 4
    assert body["byte_limit"] == 4
    assert body["raw_content"] == "4567"
    assert body["byte_count"] == 4
    assert body["artifact"]["bytes"] == 16
    assert body["next_byte_offset"] == 8
    assert body["truncated"] is True
    assert body["artifact"]["sha256"] == artifact.sha256

    tail, _ = _read_body(store, tmp_path, path=uri, byte_offset=8, byte_limit=8)
    assert tail["raw_content"] == "89abcdef"
    assert tail["next_byte_offset"] is None
    assert tail["truncated"] is False


def test_unknown_artifact_id_is_refused(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    tool = next(tool for tool in create_plugin({}).tools if tool.definition.name == "read")
    result = tool.invoke(
        ToolCall(tool_name="read", arguments={"path": f"artifact://{'0' * 64}"}),
        context=_context(tmp_path),
        services=cast(Any, store),
    )
    assert isinstance(result, ToolFailure)
    assert "no such resource" in result.error


def test_invalid_artifact_id_is_refused(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    tool = next(tool for tool in create_plugin({}).tools if tool.definition.name == "read")
    for uri, fragment in (("artifact://not-a-sha256", "invalid artifact id"), ("artifact://", "requires an artifact id")):
        result = tool.invoke(ToolCall(tool_name="read", arguments={"path": uri}), context=_context(tmp_path), services=cast(Any, store))
        assert isinstance(result, ToolFailure)
        assert result.diagnostics is not None
        assert result.diagnostics.kind == "tool_input_mismatch"
        assert result.diagnostics.details["reason"] == "invalid_arguments"
        assert fragment in result.error


def test_extensionless_raster_files_use_magic_mime(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    png = rgb_png(16, 16)
    (tmp_path / "screen").write_bytes(png)
    body, output = _read_attachment(store, tmp_path, path="screen")
    assert output.mime == "image/png"
    assert output.data_uri is None
    assert output.bounds.reference is not None
    assert output.bounds.reference.sha256 == hashlib.sha256(png).hexdigest()
    assert body["type"] == "attachment"
    assert body["artifact"]["bytes"] == len(png)
    assert store.spills == ["image/png"]

    (tmp_path / "photo").write_bytes(_JPEG_BYTES)
    _, jpeg = _read_attachment(store, tmp_path, path="photo")
    assert jpeg.mime == "image/jpeg"
    assert store.spills == ["image/png", "image/jpeg"]


def test_non_raster_suffix_with_raster_magic_uses_proven_mime(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    png = rgb_png(8, 8)
    (tmp_path / "chart.txt").write_bytes(png)
    body, output = _read_attachment(store, tmp_path, path="chart.txt")
    assert output.mime == "image/png"
    assert body["artifact"]["sha256"] == hashlib.sha256(png).hexdigest()
    assert store.spills == ["image/png"]


def test_png_suffix_still_attaches_matching_png(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    png = rgb_png(8, 8)
    (tmp_path / "screen.png").write_bytes(png)
    _, output = _read_attachment(store, tmp_path, path="screen.png")
    assert output.mime == "image/png"
    assert store.spills == ["image/png"]


def test_raster_suffix_must_match_magic(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    (tmp_path / "wrong.png").write_bytes(_JPEG_BYTES)
    failure = _read_failure(store, tmp_path, path="wrong.png")
    assert failure.diagnostics is not None
    assert failure.diagnostics.kind == "tool_input_mismatch"
    assert failure.diagnostics.details["reason"] == "invalid_image"
    assert "declared image/png but bytes are image/jpeg" in failure.error
    assert store.spills == []

    (tmp_path / "wrong.jpg").write_bytes(rgb_png(4, 4))
    failure = _read_failure(store, tmp_path, path="wrong.jpg")
    assert failure.diagnostics is not None
    assert failure.diagnostics.details["reason"] == "invalid_image"
    assert "declared image/jpeg but bytes are image/png" in failure.error
    assert store.spills == []


def test_empty_truncated_or_nonimage_raster_is_typed_failure(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    cases = {
        "empty.png": b"",
        "text.png": b"not an image",
        "truncated.png": b"\x89PNG\r\n\x1a",
        "signature_only.png": b"\x89PNG\r\n\x1a\n",
        "truncated.jpg": b"\xff\xd8\xff",
    }
    for name, payload in cases.items():
        (tmp_path / name).write_bytes(payload)
        failure = _read_failure(store, tmp_path, path=name)
        assert failure.diagnostics is not None
        assert failure.diagnostics.kind == "tool_input_mismatch"
        assert failure.diagnostics.details["reason"] == "invalid_image"
        assert "read cannot use" in failure.error
        assert failure.diagnostics.details["path"] == name
    assert store.spills == []


def test_image_artifact_renders_as_attachment(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    png = rgb_png(16, 16)
    artifact = store.spill_artifact(png, media_type="image/png")
    sha256 = artifact.sha256
    uri = f"artifact://{sha256}"

    body, output = _read_attachment(store, tmp_path, path=uri)

    assert output.mime == "image/png"
    assert output.data_uri is None
    assert output.bounds.reference is not None
    assert output.bounds.reference.uri == uri
    assert output.bounds.truncated is False
    assert body == {"path": uri, "type": "attachment", "artifact": artifact.as_payload()}
    # The artifact is its own reference; reads never duplicate it.
    assert store.spills == ["image/png"]


def test_large_image_artifact_reuses_stored_reference(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    png = rgb_png()
    artifact = store.spill_artifact(png, media_type="image/png")
    sha256 = artifact.sha256
    uri = f"artifact://{sha256}"

    body, output = _read_attachment(store, tmp_path, path=uri)

    assert output.data_uri is None
    assert uri in (output.presentation or "")
    assert output.bounds.reference is not None
    assert output.bounds.reference.uri == uri
    assert output.bounds.truncated is False
    assert output.bounds.partial is False
    assert body == {"path": uri, "type": "attachment", "artifact": artifact.as_payload()}
    # Reading an existing artifact must not store its bytes again.
    assert store.spills == ["image/png"]


def test_text_artifact_still_pages_as_text(tmp_path: Path) -> None:
    store = FakeArtifactStore()
    artifact = store.spill_artifact("first\nsecond\nthird\n", media_type="text/plain")
    uri = f"artifact://{artifact.sha256}"

    body, output = _read_body(store, tmp_path, path=uri, offset=2, limit=1)

    assert body["type"] == "artifact"
    assert body["offset_unit"] == "lines"
    assert body["line_count"] == 1
    assert body["raw_content"] == "second\n"
    assert body["next_offset"] == 3
    assert body["truncated"] is True
    assert "second" in output
