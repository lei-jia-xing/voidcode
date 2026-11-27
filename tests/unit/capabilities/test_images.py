"""Unit tests for shared Pillow raster image guards and non-PNG Kitty viewer rendering."""

from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from PIL import Image

from voidcode.capabilities.images import (
    ImageSafetyError,
    prepare_kitty_image,
    raster_format_label,
    validate_raster_stream,
)
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.tui.artifact_viewer import ArtifactSource, InteractiveArtifactViewer


def _encode(img: Image.Image, fmt: str, **save_kwargs: object) -> bytes:
    buf = BytesIO()
    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def test_validate_raster_stream_supports_all_formats_and_keeps_caller_stream_open() -> None:
    for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")):
        raw = _encode(Image.new("RGB", (6, 4), (10, 20, 30)), fmt)
        stream = BytesIO(raw)
        detected, width, height = validate_raster_stream(stream, expected_mime=mime, verify=True)
        assert (detected, width, height) == (mime, 6, 4)
        assert not stream.closed


def test_validate_raster_stream_blocks_dimension_and_pixel_bombs_before_decode() -> None:
    png = _encode(Image.new("RGB", (32, 32), (1, 2, 3)), "PNG")
    with pytest.raises(ImageSafetyError, match="axis limit"):
        validate_raster_stream(png, expected_mime="image/png", max_dimension=16)
    with pytest.raises(ImageSafetyError, match="pixel count"):
        validate_raster_stream(png, expected_mime="image/png", max_pixels=500)


def test_validate_raster_stream_rejects_corrupt_idat_when_verify_enabled() -> None:
    png = bytearray(_encode(Image.new("RGB", (8, 8), (5, 6, 7)), "PNG"))
    idat_idx = png.find(b"IDAT")
    assert idat_idx > 0
    png[idat_idx + 4 : idat_idx + 6] = b"\x00\x00"
    corrupt = bytes(png)

    # Header-only validation (verify=False) preserves Kitty-side IDAT CRC diagnosis.
    assert validate_raster_stream(corrupt, expected_mime="image/png", verify=False) == ("image/png", 8, 8)
    with pytest.raises(ImageSafetyError, match="invalid or truncated"):
        validate_raster_stream(corrupt, expected_mime="image/png", verify=True)


def test_prepare_kitty_image_keeps_png_verbatim_and_transcodes_jpeg_webp_gif() -> None:
    png_raw = _encode(Image.new("RGBA", (5, 7), (10, 20, 30, 128)), "PNG")
    dims, out_png = prepare_kitty_image(png_raw, media_type="image/png")
    assert dims == (5, 7)
    assert out_png is png_raw

    # JPEG with EXIF Orientation=6 (rotate 90 CCW / 270 CW): 6x4 raw becomes 4x6 oriented.
    jpg_img = Image.new("RGB", (6, 4), (200, 100, 50))
    exif = Image.Exif()
    exif[274] = 6
    jpg_raw = _encode(jpg_img, "JPEG", exif=exif)
    jpg_dims, jpg_kitty = prepare_kitty_image(jpg_raw, media_type="image/jpeg")
    assert jpg_dims == (4, 6)
    assert validate_raster_stream(jpg_kitty, expected_mime="image/png", verify=True) == ("image/png", 4, 6)

    for fmt, mime in (("WEBP", "image/webp"), ("GIF", "image/gif")):
        raw = _encode(Image.new("RGBA", (9, 5), (0, 255, 0, 200)), fmt)
        fmt_dims, kitty_bytes = prepare_kitty_image(raw, media_type=mime)
        assert fmt_dims == (9, 5)
        assert validate_raster_stream(kitty_bytes, expected_mime="image/png", verify=True) == ("image/png", 9, 5)


def test_artifact_viewer_accepts_non_png_rasters_for_kitty_preview() -> None:
    webp_raw = _encode(Image.new("RGB", (12, 8), (40, 80, 120)), "WEBP")
    sha = hashlib.sha256(webp_raw).hexdigest()
    ref = ArtifactReference(sha256=sha, bytes=len(webp_raw), media_type="image/webp")
    viewer = InteractiveArtifactViewer(
        (ArtifactSource("photo.webp", sha, ref),),
        revision_id="rev-1",
        session_id="s1",
        title="WebP Artifact",
    )
    assert viewer.is_raster_image is True
    assert raster_format_label(viewer.media_type) == "WebP"

    req_id = viewer.begin_loading(0)
    assert viewer.accept_page(req_id, ArtifactPage(artifact=ref, offset=0, data=webp_raw, eof=True))
    assert viewer.image_dimensions == (12, 8)

    img_req = viewer.begin_image_load()
    dims, kitty_png = prepare_kitty_image(webp_raw, media_type="image/webp")
    assert dims == (12, 8)
    assert viewer.accept_image(img_req, sha, kitty_png)
    assert viewer.image_status == "transferring"
    viewer.mark_image_uploaded(sha)
    rendered = "\n".join(viewer.render_rows(80, max_rows=30))
    assert "WebP geometry: 12×8" in rendered
    assert "Kitty Graphics preview" in rendered
