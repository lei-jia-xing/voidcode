"""Paged immutable sources, strict incremental UTF-8, and explicit PNG/open states.

Only page/metadata state lives here. Network reads and external exports run in
workers; terminal PNG decoding is confirmed by a correlated Kitty reply, never
by the IHDR geometry reader. Closing a view invalidates its request identities.
"""

from __future__ import annotations

import codecs
import hashlib
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from rich.text import Text

from voidcode.sdk.client import SessionClient
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.sdk.tools import CompositeOutput, JsonPart, MediaPart, TablePart, TextPart, ToolResult

from .keys import Key
from .region import KittyImagePlacement
from .styles import Styles, markdown_theme
from .term import escape_terminal_controls, render_lines


@dataclass(frozen=True, slots=True)
class ArtifactSource:
    """One immutable source chosen from a tool result, never a path."""

    label: str
    sha256: str
    reference: ArtifactReference | None = None

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("artifact source requires a lowercase sha256 digest")
        if self.reference is not None and self.reference.sha256 != self.sha256:
            raise ValueError("artifact source reference digest does not match")

    @property
    def uri(self) -> str:
        return f"artifact://{self.sha256}"

    @property
    def total_bytes(self) -> int | None:
        return self.reference.bytes if self.reference is not None else None

    @property
    def media_type(self) -> str:
        return self.reference.media_type if self.reference is not None else "application/octet-stream"


def artifact_sources(result: ToolResult) -> tuple[ArtifactSource, ...]:
    """Discover typed references without classifying tools or relabelling selected text."""
    sources: list[ArtifactSource] = []
    seen: set[str] = set()

    def record(label: str, digest: str, reference: ArtifactReference | None) -> bool:
        try:
            source = ArtifactSource(label, digest, reference)
        except ValueError:
            return False
        if digest not in seen:
            sources.append(source)
            seen.add(digest)
        return True

    def add_reference(label: str, value: ArtifactReference | str | None) -> None:
        if isinstance(value, ArtifactReference):
            record(label, value.sha256, value)
        elif isinstance(value, str) and value.startswith("artifact://"):
            record(label, value.removeprefix("artifact://"), None)

    def add_raw(label: str, value: object) -> bool:
        if isinstance(value, Mapping):
            digest, total, media = value.get("sha256"), value.get("bytes"), value.get("media_type")
            if not isinstance(digest, str) or type(total) is not int or total < 0 or not isinstance(media, str):
                return False
            return record(label, digest, ArtifactReference(sha256=digest, bytes=total, media_type=media))
        if isinstance(value, str) and value.startswith("artifact://"):
            return record(label, value.removeprefix("artifact://"), None)
        return False

    def visit(value: object, path: str) -> None:
        if add_raw(path, value):
            return
        if isinstance(value, Mapping):
            for name, child in value.items():
                visit(child, f"{path}.{name}")
        elif isinstance(value, (tuple, list)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    output = result.output
    add_reference("Original output source", output.bounds.reference)
    if isinstance(output, CompositeOutput):
        for index, part in enumerate(output.parts):
            if isinstance(part, TextPart):
                kind, reference = "text", part.reference
            elif isinstance(part, MediaPart):
                kind, reference = "media", part.reference
            elif isinstance(part, TablePart):
                kind, reference = "table", part.reference
            elif isinstance(part, JsonPart):
                kind, reference = "json", part.reference
            else:
                continue
            add_reference(f"Part {index} ({kind})", reference)
    if result.body is not None:
        visit(result.body.as_payload(), "Result body")
    add_reference("Complete structured result JSON", result.result_reference)
    return tuple(sources)


def read_artifact_bytes(
    client: SessionClient,
    sha256: str,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bytes:
    """Read immutable bytes using the public page API, off the UI thread."""
    data = bytearray()
    reference: ArtifactReference | None = None
    while True:
        if cancelled is not None and cancelled():
            raise InterruptedError("artifact view closed or changed")
        page = client.read_artifact(sha256, offset=len(data))
        if page.offset != len(data) or page.artifact.sha256 != sha256:
            raise ValueError("artifact response changed source or offset")
        if reference is not None and reference != page.artifact:
            raise ValueError("immutable artifact metadata changed between pages")
        reference = page.artifact
        end = page.offset + len(page.data)
        if end > reference.bytes or (not page.data and not page.eof) or page.eof != (end >= reference.bytes):
            raise ValueError("artifact response has inconsistent byte progress/EOF")
        data.extend(page.data)
        if progress is not None:
            progress(len(data))
        if page.eof:
            if hashlib.sha256(data).hexdigest() != sha256:
                raise ValueError("artifact bytes do not match their immutable SHA-256 identity")
            return bytes(data)


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read only the PNG IHDR geometry; terminal upload acknowledgement validates decoding."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width == 0 or height == 0:
        return None
    return width, height


def _styles_arg(styles: Styles | None) -> Styles:
    return styles if styles is not None else Styles()


class InteractiveArtifactViewer:
    """A single artifact page at a time, with source identity and honest load state."""

    def __init__(
        self,
        sources: tuple[ArtifactSource, ...],
        *,
        revision_id: str,
        session_id: str,
        title: str,
    ) -> None:
        if not sources:
            raise ValueError("artifact viewer requires at least one source")
        self.sources = sources
        self.revision_id = revision_id
        self.session_id = session_id
        self.title = title
        self.identity = uuid4().hex
        self.source_index = 0
        self.request_id = 0
        self.loading_offset: int | None = None
        self.load_error: str | None = None
        self.page: ArtifactPage | None = None
        self._text_pages: dict[tuple[str, int], str | None] = {}
        self._decoder_states: dict[str, dict[int, tuple[bytes, int]]] = {source.sha256: {0: (b"", 0)} for source in sources}
        self._history: list[int] = []
        self._history_index = -1
        self.scroll = 0
        self._max_scroll = 0
        self.revision_notice: str | None = None
        self.connection_notice: str | None = None
        self.image_status = "unavailable"
        self.image_error: str | None = None
        self.image_bytes: bytes | None = None
        self.image_dimensions: tuple[int, int] | None = None
        self.image_request_id = 0
        self.image_bytes_received = 0
        self.open_status: str | None = None
        self.open_request_id = 0
        self._image_row = 0
        self._image_rows = 0

    @property
    def source(self) -> ArtifactSource:
        return self.sources[self.source_index]

    @property
    def image_row(self) -> int:
        return self._image_row

    @property
    def media_type(self) -> str:
        if self.page is not None:
            return self.page.artifact.media_type
        return self.source.media_type

    @property
    def total_bytes(self) -> int | None:
        if self.page is not None:
            return self.page.artifact.bytes
        return self.source.total_bytes

    @property
    def is_png(self) -> bool:
        return self.media_type.lower().split(";", 1)[0].strip() == "image/png"

    @property
    def displayed_text(self) -> str | None:
        if self.page is None:
            return None
        return self._text_pages.get((self.source.sha256, self.page.offset))

    def begin_loading(self, offset: int) -> int:
        self.request_id += 1
        self.loading_offset = offset
        self.load_error = None
        self.page = None
        self.scroll = 0
        return self.request_id

    def accept_page(self, request_id: int, page: ArtifactPage) -> bool:
        if request_id != self.request_id or self.loading_offset != page.offset or page.artifact.sha256 != self.source.sha256:
            return False
        end = page.offset + len(page.data)
        if page.data and end > page.artifact.bytes:
            self.reject_page(request_id, "Artifact page exceeds its declared byte range")
            return True
        if page.offset < page.artifact.bytes and not page.data:
            self.reject_page(request_id, "Artifact page made no progress before EOF")
            return True
        if page.eof != (end >= page.artifact.bytes):
            self.reject_page(request_id, "Artifact page EOF does not match its byte range")
            return True
        source = self.source
        if source.reference is not None and source.reference != page.artifact:
            self.reject_page(request_id, "Immutable artifact metadata changed")
            return True
        if source.reference is None:
            sources = list(self.sources)
            sources[self.source_index] = ArtifactSource(source.label, source.sha256, page.artifact)
            self.sources = tuple(sources)
        self._text_pages.clear()
        self._decode_page(page)
        if page.offset == 0 and page.artifact.media_type.lower().split(";", 1)[0].strip() == "image/png":
            self.image_dimensions = png_dimensions(page.data)
        self.page = page
        self.loading_offset = None
        self.load_error = None
        if page.offset in self._history:
            self._history_index = self._history.index(page.offset)
        else:
            self._history = self._history[: self._history_index + 1]
            self._history.append(page.offset)
            self._history_index = len(self._history) - 1
        return True

    def reject_page(self, request_id: int, message: str) -> bool:
        if request_id != self.request_id:
            return False
        self.loading_offset = None
        self.load_error = message
        self.page = None
        return True

    def next_offset(self) -> int | None:
        if self.page is None or self.page.eof or not self.page.data or self.loading_offset is not None:
            return None
        return self.page.offset + len(self.page.data)

    def previous_offset(self) -> int | None:
        if self.loading_offset is not None or self._history_index <= 0:
            return None
        return self._history[self._history_index - 1]

    def select_source(self, delta: int) -> int | None:
        if len(self.sources) < 2 or self.loading_offset is not None:
            return None
        self.source_index = (self.source_index + delta) % len(self.sources)
        self.request_id += 1
        self.page = None
        self.loading_offset = None
        self.load_error = None
        self._history = []
        self._history_index = -1
        self.scroll = 0
        self.image_request_id += 1
        self.image_bytes = None
        self.image_dimensions = None
        self.image_error = None
        self.image_status = "unavailable"
        self.image_bytes_received = 0
        self.open_request_id += 1
        self.open_status = None
        return 0

    def mark_revision_changed(self, revision_id: str) -> None:
        if revision_id != self.revision_id:
            self.revision_notice = f"Current revision changed to {revision_id}; this immutable artifact remains workspace-scoped."

    def begin_image_load(self) -> int:
        self.image_request_id += 1
        self.image_status = "loading"
        self.image_error = None
        self.image_bytes_received = 0
        return self.image_request_id

    def update_image_progress(self, request_id: int, sha256: str, received: int) -> bool:
        if request_id != self.image_request_id or sha256 != self.source.sha256:
            return False
        self.image_bytes_received = received
        return True

    def accept_image(self, request_id: int, sha256: str, data: bytes) -> bool:
        if request_id != self.image_request_id or sha256 != self.source.sha256:
            return False
        expected_bytes = self.total_bytes
        if expected_bytes is not None and len(data) != expected_bytes:
            self.image_status = "error"
            self.image_error = f"Image page total changed: expected {expected_bytes} bytes, received {len(data)}"
            return True
        dimensions = png_dimensions(data)
        if dimensions is None:
            self.image_status = "error"
            self.image_error = "PNG artifact has no valid IHDR geometry; original bytes remain available via Open."
            return True
        self.image_bytes = data
        self.image_dimensions = dimensions
        self.image_status = "transferring"
        self.image_error = None
        return True

    def mark_image_uploaded(self, sha256: str) -> None:
        if sha256 == self.source.sha256:
            self.image_bytes = None
            self.image_status = "ready"
            self.image_error = None

    def mark_image_cached(self, sha256: str, dimensions: tuple[int, int] | None) -> None:
        if sha256 == self.source.sha256 and dimensions is not None:
            self.image_dimensions = dimensions
            self.image_status = "ready"
            self.image_error = None
            self.image_bytes_received = 0

    def fail_image(self, sha256: str, message: str) -> None:
        if sha256 == self.source.sha256:
            self.image_status = "error"
            self.image_error = message

    def begin_open(self) -> int:
        self.open_request_id += 1
        self.open_status = "Opening artifact copy…"
        return self.open_request_id

    def finish_open(self, request_id: int, message: str) -> bool:
        if request_id != self.open_request_id:
            return False
        self.open_status = message
        return True

    def image_placement(
        self,
        *,
        max_columns: int,
        max_rows: int,
        cell_pixels: tuple[float, float] | None,
        placement_id: int,
    ) -> KittyImagePlacement | None:
        dimensions = self.image_dimensions
        if self.image_status != "ready" or dimensions is None or not self.is_png or cell_pixels is None or max_rows <= 0 or max_columns <= 0:
            return None
        cell_width, cell_height = cell_pixels
        if cell_width <= 0 or cell_height <= 0:
            return None
        image_width, image_height = dimensions
        height_fit_columns = int(max_rows * cell_height * image_width / (cell_width * image_height))
        columns = min(max_columns, height_fit_columns)
        if columns <= 0:
            return None
        return KittyImagePlacement(
            sha256=self.source.sha256,
            row=self._image_row,
            column=0,
            columns=columns,
            placement_id=placement_id,
        )

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name == "esc":
            self.request_id += 1
            self.image_request_id += 1
            self.open_request_id += 1
            return {"cancel": True}
        if key.name in ("up", "down"):
            self.scroll = max(0, min(self._max_scroll, self.scroll + (-1 if key.name == "up" else 1)))
        elif key.name in ("pageUp", "left", "p"):
            offset = self.previous_offset()
            if offset is not None:
                return {"action": "page", "offset": offset}
        elif key.name in ("pageDown", "right", "n"):
            offset = self.next_offset()
            if offset is not None:
                return {"action": "page", "offset": offset}
        elif key.name == "tab":
            offset = self.select_source(1)
            if offset is not None:
                return {"action": "page", "offset": offset}
        elif key.name == "o":
            return {"action": "open"}
        return None

    def render_rows(self, width: int, styles: Styles | None = None, *, max_rows: int = 22) -> list[str]:
        resolved = _styles_arg(styles)
        row_width = max(1, width)
        source_label = f"{self.source.label} ({self.source_index + 1}/{len(self.sources)})"
        title_rows = render_lines(
            Text(escape_terminal_controls(f"{self.title} · {source_label}"), style=resolved.style("accent")),
            row_width,
            theme=markdown_theme(),
            color_system=resolved.color_system,
        )
        uri_rows = render_lines(
            Text(escape_terminal_controls(f"URI: {self.source.uri}"), style=resolved.style("text")),
            row_width,
            theme=markdown_theme(),
            color_system=resolved.color_system,
        )
        rows = [*title_rows, *uri_rows, escape_terminal_controls(f"Media type: {self.media_type}")]
        if self.page is not None:
            end = self.page.offset + len(self.page.data)
            page_number = self._history_index + 1
            rows.append(
                f"Page {page_number} · bytes [{self.page.offset:,}, {end:,}) / {self.page.artifact.bytes:,} · EOF {'yes' if self.page.eof else 'no'}"
            )
        elif self.loading_offset is not None:
            rows.append(f"Loading byte page at offset {self.loading_offset:,}…")
        elif self.load_error is not None:
            rows.append(resolved.fg("error", f"Artifact page load failed: {escape_terminal_controls(self.load_error)}"))
        else:
            rows.append("Artifact page not loaded")
        rows.append("↑/↓ scroll · PgUp/P prev · PgDn/N next · Tab source · O open · Esc close")
        if self.revision_notice:
            rows.append(resolved.fg("warning", escape_terminal_controls(self.revision_notice)))
        if self.connection_notice:
            rows.append(resolved.fg("warning", escape_terminal_controls(self.connection_notice)))
        if self.open_status:
            rows.append(escape_terminal_controls(self.open_status))
        if self.image_error:
            rows.append(resolved.fg("error", escape_terminal_controls(self.image_error)))
        self._image_row = len(rows)
        self._image_rows = 0
        if self.is_png:
            if self.image_dimensions is not None:
                image_width, image_height = self.image_dimensions
                rows.append(f"PNG declared geometry: {image_width}×{image_height} · O opens original copy")
            if self.image_status == "loading":
                total = "?" if self.total_bytes is None else f"{self.total_bytes:,}"
                rows.append(f"Fetching PNG artifact pages: {self.image_bytes_received:,} / {total} bytes…")
                self._image_row = len(rows)
            elif self.image_status == "transferring":
                rows.append("Validating and transmitting PNG to Kitty Graphics…")
            elif self.image_status == "ready" and self.image_dimensions is not None:
                rows.append("Kitty Graphics preview (terminal decoding acknowledged)")
                self._image_row = len(rows)
                self._image_rows = max(0, max_rows - len(rows))
                rows.extend([""] * self._image_rows)
            elif self.image_status == "error":
                rows.append("PNG preview unavailable; original artifact URI and Open action remain available.")
                self._image_row = len(rows)
            else:
                rows.append("PNG preview capability/image load not available; O opens original bytes.")
                self._image_row = len(rows)
        self._max_scroll = 0
        if self.page is not None and not self.is_png:
            content = self.displayed_text
            if content is None:
                content = self._hex_page(self.page)
                header = "Binary bytes (hex; source bytes preserved):"
            else:
                header = "Artifact page text:"
            body = render_lines(
                Text(escape_terminal_controls(f"{header}\n{content}", preserve_newlines=True, preserve_tabs=True), style=resolved.style("text")),
                row_width,
                theme=markdown_theme(),
                color_system=resolved.color_system,
            )
            available = max(0, max_rows - len(rows))
            self._max_scroll = max(0, len(body) - available)
            self.scroll = min(self.scroll, self._max_scroll)
            rows.extend(body[self.scroll : self.scroll + available])
        return rows[: max(1, max_rows)]

    def _decode_page(self, page: ArtifactPage) -> None:
        sha256 = page.artifact.sha256
        media_type = page.artifact.media_type.lower().split(";", 1)[0].strip()
        if media_type.startswith("image/") or not (
            media_type.startswith("text/")
            or media_type == "application/json"
            or media_type.endswith("+json")
            or media_type in {"application/xml", "application/yaml", "application/x-yaml"}
        ):
            self._text_pages[(sha256, page.offset)] = None
            return
        states = self._decoder_states[sha256]
        before = states.get(page.offset)
        if before is None:
            self._text_pages[(sha256, page.offset)] = None
            return
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        decoder.setstate(before)
        try:
            text = decoder.decode(page.data, final=page.eof)
        except UnicodeDecodeError:
            self._text_pages[(sha256, page.offset)] = None
            return
        self._text_pages[(sha256, page.offset)] = text
        states[page.offset + len(page.data)] = decoder.getstate()

    @staticmethod
    def _hex_page(page: ArtifactPage) -> str:
        lines = []
        for start in range(0, len(page.data), 16):
            chunk = page.data[start : start + 16]
            lines.append(f"{page.offset + start:08x}  " + " ".join(f"{byte:02x}" for byte in chunk))
        return "\n".join(lines)
