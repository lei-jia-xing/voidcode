"""Shared Pillow-backed raster image inspection, safety bounds, and Kitty PNG transcoding."""

from __future__ import annotations

import os
from io import BytesIO
from typing import IO, Final

from PIL import Image, ImageOps

#: Hard dimension and pixel-count bounds to defeat decompression bombs before decoding.
MAX_RASTER_PIXELS: Final[int] = 40_000_000
MAX_RASTER_DIMENSION: Final[int] = 16_384
#: Upper raw-file bound on tool/artifact raster ingestion before spilling/loading into RAM.
MAX_RASTER_BYTES: Final[int] = 32 * 1024 * 1024

# Set Pillow's internal guard slightly above MAX_RASTER_PIXELS once at import time so our
# deterministic pre-decode check reports exact counts cleanly without mutating globals per call.
Image.MAX_IMAGE_PIXELS = MAX_RASTER_PIXELS * 2

_MIME_TO_PIL_FORMAT: Final[dict[str, str]] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}
_PIL_FORMAT_TO_MIME: Final[dict[str, str]] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_FORMAT_LABELS: Final[dict[str, str]] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WebP",
}
_SUPPORTED_PIL_FORMATS: Final[tuple[str, ...]] = ("PNG", "JPEG", "GIF", "WEBP")


class ImageSafetyError(ValueError):
    """A raster image is malformed, mismatched, truncated, or exceeds pixel/size safety limits."""


def normalize_raster_mime(media_type: str) -> str | None:
    """Canonical supported raster MIME type, or ``None`` for unsupported types."""
    base = media_type.lower().split(";", 1)[0].strip()
    if base == "image/jpg":
        base = "image/jpeg"
    return base if base in _FORMAT_LABELS else None


def raster_format_label(media_type: str) -> str:
    """Short human display label for a raster media type."""
    normalized = normalize_raster_mime(media_type)
    return _FORMAT_LABELS.get(normalized or "", "Image")


def _check_header(
    img: Image.Image,
    *,
    expected_mime: str | None,
    max_pixels: int,
    max_dimension: int,
) -> tuple[str, int, int]:
    fmt = img.format
    if fmt is None or fmt not in _PIL_FORMAT_TO_MIME:
        raise ImageSafetyError("image format is not supported")
    actual_mime = _PIL_FORMAT_TO_MIME[fmt]
    if expected_mime is not None:
        normalized = normalize_raster_mime(expected_mime)
        if normalized != actual_mime:
            raise ImageSafetyError(f"declared {normalized or expected_mime} but container is {actual_mime}")
    width, height = img.size
    if width <= 0 or height <= 0:
        raise ImageSafetyError("image has invalid zero dimensions")
    if max(width, height) > max_dimension:
        raise ImageSafetyError(f"image dimensions {width}x{height} exceed the {max_dimension}px axis limit")
    if width * height > max_pixels:
        raise ImageSafetyError(f"image pixel count {width * height:,} exceeds the {max_pixels:,}-pixel limit")
    return actual_mime, width, height


def validate_raster_stream(
    source: IO[bytes] | bytes | bytearray,
    *,
    expected_mime: str | None = None,
    verify: bool = True,
    max_pixels: int | None = None,
    max_dimension: int | None = None,
) -> tuple[str, int, int]:
    """Validate raster container headers, bounds, and (when ``verify=True``) container integrity.

    Does not close a caller-supplied file stream.
    """
    resolved_max_pixels = MAX_RASTER_PIXELS if max_pixels is None else max_pixels
    resolved_max_dimension = MAX_RASTER_DIMENSION if max_dimension is None else max_dimension
    stream = BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    formats = (_MIME_TO_PIL_FORMAT[normalized],) if expected_mime and (normalized := normalize_raster_mime(expected_mime)) else _SUPPORTED_PIL_FORMATS
    try:
        with Image.open(stream, formats=formats) as img:
            actual_mime, width, height = _check_header(
                img,
                expected_mime=expected_mime,
                max_pixels=resolved_max_pixels,
                max_dimension=resolved_max_dimension,
            )
            if verify:
                img.verify()
    except ImageSafetyError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageSafetyError(f"image exceeds pixel safety limit: {exc}") from exc
    except (Image.UnidentifiedImageError, OSError, SyntaxError, EOFError, ValueError) as exc:
        raise ImageSafetyError(f"invalid or truncated raster container: {exc}") from exc
    return actual_mime, width, height


def check_raster_file_size(source: IO[bytes], *, max_bytes: int | None = None) -> int:
    """Return file byte size from *source* descriptor or raise ``ImageSafetyError`` if over *max_bytes*."""
    resolved_max_bytes = MAX_RASTER_BYTES if max_bytes is None else max_bytes
    size = os.fstat(source.fileno()).st_size
    if size > resolved_max_bytes:
        raise ImageSafetyError(f"image size {size:,} bytes exceeds the {resolved_max_bytes:,}-byte limit")
    return size


def prepare_kitty_image(
    data: bytes,
    *,
    media_type: str,
    max_pixels: int | None = None,
    max_dimension: int | None = None,
) -> tuple[tuple[int, int], bytes]:
    """Return ``((width, height), png_bytes)`` ready for Kitty Graphics transmission.

    Original PNG bytes are returned unmodified (only header-checked so terminal decoding can
    report corrupt IDAT chunks); JPEG, WebP, and GIF images are decoded under pixel bounds and
    encoded to PNG for Kitty transmission.
    """
    resolved_max_pixels = MAX_RASTER_PIXELS if max_pixels is None else max_pixels
    resolved_max_dimension = MAX_RASTER_DIMENSION if max_dimension is None else max_dimension
    normalized = normalize_raster_mime(media_type)
    if normalized is None:
        raise ImageSafetyError(f"unsupported raster media type: {media_type}")
    if normalized == "image/png":
        _, width, height = validate_raster_stream(
            data,
            expected_mime="image/png",
            verify=False,
            max_pixels=resolved_max_pixels,
            max_dimension=resolved_max_dimension,
        )
        return (width, height), data

    pil_format = _MIME_TO_PIL_FORMAT[normalized]
    try:
        with Image.open(BytesIO(data), formats=(pil_format,)) as img:
            _check_header(
                img,
                expected_mime=normalized,
                max_pixels=resolved_max_pixels,
                max_dimension=resolved_max_dimension,
            )
            img.load()
            oriented = ImageOps.exif_transpose(img) if normalized == "image/jpeg" else img
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            target_mode = "RGBA" if has_alpha else "RGB"
            converted = oriented if oriented.mode == target_mode else oriented.convert(target_mode)
            width, height = converted.size
            if max(width, height) > resolved_max_dimension or width * height > resolved_max_pixels:
                raise ImageSafetyError(f"image dimensions {width}x{height} exceed safety limits")
            out = BytesIO()
            converted.save(out, format="PNG")
    except ImageSafetyError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageSafetyError(f"image exceeds pixel safety limit: {exc}") from exc
    except (Image.UnidentifiedImageError, OSError, SyntaxError, EOFError, ValueError) as exc:
        raise ImageSafetyError(f"invalid or truncated raster image: {exc}") from exc
    return (width, height), out.getvalue()
