"""Bounded image attachments for the inline TUI composer.

Path and clipboard ingress share one validator: the extension/media type, the
bytes themselves, the per-image bound, the total bound, and the pending count
all fail visibly before an ``ImageAttachment`` reaches ``RunSpec``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from voidcode.sdk.provider import ImageAttachment
from voidcode.sdk.tools import RASTER_IMAGE_MIMES

#: Pending images are bounded by provider-visible ingress, not terminal memory:
#: the JSON-RPC envelope is 4 MiB, so base64 plus prompt must stay below it.
MAX_PENDING_IMAGES: Final[int] = 4
MAX_IMAGE_BYTES: Final[int] = 2 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES: Final[int] = 2 * 1024 * 1024

_EXTENSION_MEDIA_TYPES: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_SUPPORTED_NAMES: Final[str] = "PNG, JPEG, GIF, or WebP"


class AttachmentError(ValueError):
    """An image cannot be attached; the message names the refused rule."""


def format_bytes(size: int) -> str:
    """Compact human byte count for the pending-attachment indicator."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def sniff_image_media_type(raw: bytes) -> str | None:
    """The raster MIME type proven by magic bytes, or ``None`` for anything else."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalized_media_type(media_type: str) -> str:
    normalized = media_type.strip().lower().split(";", 1)[0]
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in RASTER_IMAGE_MIMES:
        raise AttachmentError(f"{media_type!r} is not a supported raster image ({_SUPPORTED_NAMES})")
    return normalized


def _decoded_size(data: str) -> int:
    try:
        return len(base64.b64decode(data, validate=True))
    except ValueError:
        return len(data) * 3 // 4


@dataclass(frozen=True, slots=True)
class PendingImage:
    """One validated attachment waiting for the next composer prompt."""

    attachment: ImageAttachment
    label: str
    bytes: int

    @property
    def media_type(self) -> str:
        return self.attachment.media_type

    @classmethod
    def from_attachment(cls, attachment: ImageAttachment, *, label: str = "restored image") -> PendingImage:
        return cls(attachment, label, _decoded_size(attachment.data))


@dataclass(slots=True)
class PendingAttachments:
    """The composer's ordered pending images with one enforcing add path."""

    _items: list[PendingImage] = field(default_factory=list)

    @property
    def items(self) -> tuple[PendingImage, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self._items)

    def add(self, image: PendingImage) -> None:
        """Append one validated image or raise the visible rule it violates."""
        normalized = _normalized_media_type(image.attachment.media_type)
        if normalized != image.attachment.media_type:
            raise AttachmentError(f"{image.label}: media type must be written as {normalized}")
        if image.bytes <= 0:
            raise AttachmentError(f"{image.label}: image is empty")
        if image.bytes > MAX_IMAGE_BYTES:
            raise AttachmentError(f"{image.label}: {format_bytes(image.bytes)} exceeds the {format_bytes(MAX_IMAGE_BYTES)} per-image limit")
        if any(item.attachment.media_type == image.attachment.media_type and item.attachment.data == image.attachment.data for item in self._items):
            raise AttachmentError(f"{image.label} is already attached")
        if len(self._items) >= MAX_PENDING_IMAGES:
            raise AttachmentError(f"at most {MAX_PENDING_IMAGES} images can be attached to one prompt")
        total = self.total_bytes + image.bytes
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise AttachmentError(f"attachments total {format_bytes(total)}; the per-prompt limit is {format_bytes(MAX_TOTAL_IMAGE_BYTES)}")
        self._items.append(image)

    def remove_at(self, index: int) -> PendingImage:
        """Remove the zero-based item; an empty/missing index is a visible refusal."""
        if not 0 <= index < len(self._items):
            raise AttachmentError(f"no pending image at position {index + 1}")
        return self._items.pop(index)

    def remove_last(self) -> PendingImage:
        if not self._items:
            raise AttachmentError("no pending images to remove")
        return self._items.pop()

    def clear(self) -> None:
        self._items.clear()

    def take(self) -> tuple[PendingImage, ...]:
        """Detach every pending image for one dispatched prompt."""
        items = tuple(self._items)
        self._items.clear()
        return items


def pending_image_from_bytes(raw: bytes, media_type: str, label: str) -> PendingImage:
    """Validate raw clipboard/file bytes into one pending attachment."""
    normalized = _normalized_media_type(media_type)
    if not raw:
        raise AttachmentError(f"{label}: image is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise AttachmentError(f"{label}: {format_bytes(len(raw))} exceeds the {format_bytes(MAX_IMAGE_BYTES)} per-image limit")
    actual = sniff_image_media_type(raw)
    if actual is None:
        raise AttachmentError(f"{label}: contents are not a supported raster image ({_SUPPORTED_NAMES})")
    if actual != normalized:
        raise AttachmentError(f"{label}: declared {normalized} but contents are {actual}")
    data = base64.b64encode(raw).decode("ascii")
    return PendingImage(ImageAttachment(normalized, data), label, len(raw))


def load_image_file(path_text: str) -> PendingImage:
    """Read one explicitly named local image file; never guess from unrelated text."""
    text = path_text.strip()
    if not text:
        raise AttachmentError("image path is empty")
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except OSError as exc:
        raise AttachmentError(f"{text}: cannot open ({exc.strerror or exc})") from exc
    if not path.is_file():
        raise AttachmentError(f"{text}: not a regular file")
    suffix = path.suffix.lower()
    expected = _EXTENSION_MEDIA_TYPES.get(suffix)
    if expected is None:
        raise AttachmentError(f"{path.name}: unsupported image extension {suffix or '(none)'}; use {_SUPPORTED_NAMES}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AttachmentError(f"{path.name}: cannot stat ({exc.strerror or exc})") from exc
    if size == 0:
        raise AttachmentError(f"{path.name}: image is empty")
    if size > MAX_IMAGE_BYTES:
        raise AttachmentError(f"{path.name}: {format_bytes(size)} exceeds the {format_bytes(MAX_IMAGE_BYTES)} per-image limit")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AttachmentError(f"{path.name}: cannot read ({exc.strerror or exc})") from exc
    if len(raw) != size:
        raise AttachmentError(f"{path.name}: file changed while attaching")
    return pending_image_from_bytes(raw, expected, path.name)


__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_PENDING_IMAGES",
    "MAX_TOTAL_IMAGE_BYTES",
    "AttachmentError",
    "PendingAttachments",
    "PendingImage",
    "format_bytes",
    "load_image_file",
    "pending_image_from_bytes",
    "sniff_image_media_type",
]
