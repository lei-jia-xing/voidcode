"""Extracted CoreTuiApp behavior: artifact actions.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import tempfile
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from voidcode.capabilities.images import prepare_kitty_image

from . import catalog, events
from .artifact_viewer import (
    InteractiveArtifactViewer,
    read_artifact_bytes,
)
from .term import TerminalCapabilityError

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


class _ArtifactContext(TypedDict):
    """Correlation fields every artifact worker frame carries."""

    viewer: str
    session_id: str
    generation: int


class ArtifactActions:
    def _show_truncated_output(self: CoreTuiApp) -> None:
        if self._latest_tool_artifacts is None:
            self.terminal.commit_rows(
                [
                    self.styles.fg(
                        "warning",
                        "No immutable tool artifact is available; inline content is not a full-source fallback.",
                    )
                ]
            )
            return
        title, sources = self._latest_tool_artifacts
        self._close_live_input()
        viewer = InteractiveArtifactViewer(
            sources, revision_id=catalog.displayed_revision(self), session_id=self.session_id, title=f"Tool artifacts: {title}"
        )
        self._overlays.show(viewer)
        self._load_artifact_page(viewer, 0)

    def _artifact_context(self: CoreTuiApp, viewer: InteractiveArtifactViewer) -> _ArtifactContext:
        return {"viewer": viewer.identity, "session_id": self.session_id, "generation": self._generation}

    def _artifact_cancelled(self: CoreTuiApp, viewer: InteractiveArtifactViewer, sha256: str) -> bool:
        return (
            self._stop_event.is_set() or self.active_overlay is not viewer or viewer.session_id != self.session_id or viewer.source.sha256 != sha256
        )

    def _load_artifact_page(self: CoreTuiApp, viewer: InteractiveArtifactViewer, offset: int) -> None:
        request_id = viewer.begin_loading(offset)
        sha256 = viewer.source.sha256
        context = self._artifact_context(viewer)

        def worker() -> None:
            try:
                page = self.client.read_artifact(sha256, offset=offset)
                self.event_queue.put(events.ArtifactPageEvent(**context, request_id=request_id, page=page))
            except Exception as exc:
                self.event_queue.put(events.ArtifactErrorEvent(**context, request_id=request_id, error=str(exc)))

        self._work.submit("ArtifactPage", worker)

    def _load_artifact_image(self: CoreTuiApp, viewer: InteractiveArtifactViewer) -> None:
        if not viewer.is_raster_image or viewer.image_status != "unavailable":
            return
        sha256 = viewer.source.sha256
        cached = self.terminal.cached_kitty_image(sha256)
        if cached is not None:
            viewer.mark_image_cached(sha256, cached)
            return
        with self._terminal_read_lock:
            graphics_supported = self.terminal.kitty_graphics_supported
        if not graphics_supported:
            viewer.fail_image(sha256, "Kitty Graphics probe not confirmed; O explicitly opens a fetched original copy.")
            return
        if self.terminal.cell_pixel_size is None:
            viewer.fail_image(sha256, "Terminal did not report pixel/cell geometry; O explicitly opens a fetched original copy.")
            return
        request_id = viewer.begin_image_load()
        context = self._artifact_context(viewer)

        def worker() -> None:
            try:
                data = read_artifact_bytes(
                    self.client,
                    sha256,
                    progress=lambda received: self.event_queue.put(
                        events.ArtifactImageProgressEvent(**context, request_id=request_id, sha256=sha256, received=received)
                    ),
                    cancelled=lambda: self._artifact_cancelled(viewer, sha256),
                )
                expected_bytes = viewer.total_bytes
                if expected_bytes is not None and len(data) != expected_bytes:
                    raise ValueError(f"Image page total changed: expected {expected_bytes} bytes, received {len(data)}")
                dimensions, kitty_png = prepare_kitty_image(data, media_type=viewer.media_type)
                self.event_queue.put(events.ArtifactImageEvent(**context, request_id=request_id, sha256=sha256, data=kitty_png))
                if self._artifact_cancelled(viewer, sha256):
                    return
                # Probe under the lock (it reads the reply); transmit outside it:
                # holding the lock across a bulk PTY write would starve input.
                with self._terminal_read_lock:
                    supported = self.terminal.kitty_graphics_supported
                if not supported:
                    raise TerminalCapabilityError("Kitty Graphics is not supported by the active terminal")
                self.terminal.upload_kitty_png(sha256, kitty_png, dimensions)
                self.event_queue.put(events.ArtifactTransmittedEvent(**context, request_id=request_id, sha256=sha256))
            except InterruptedError:
                return
            except Exception as exc:
                self.event_queue.put(events.ArtifactImageErrorEvent(**context, request_id=request_id, sha256=sha256, error=str(exc)))

        self._work.submit("ArtifactImage", worker)

    def _open_artifact(self: CoreTuiApp, viewer: InteractiveArtifactViewer) -> None:
        request_id = viewer.begin_open()
        sha256 = viewer.source.sha256
        media_type = viewer.media_type.split(";", 1)[0]
        context = self._artifact_context(viewer)

        def worker() -> None:
            path: Path | None = None
            try:
                data = read_artifact_bytes(self.client, sha256, cancelled=lambda: self._artifact_cancelled(viewer, sha256))
                suffix = mimetypes.guess_extension(media_type) or ".bin"
                fd, filename = tempfile.mkstemp(prefix=f"{sha256}-", suffix=suffix, dir=self._artifact_exports.name)
                path = Path(filename)
                with os.fdopen(fd, "wb") as target:
                    target.write(data)
                if self._artifact_cancelled(viewer, sha256):
                    path.unlink(missing_ok=True)
                    return
                if not webbrowser.open_new_tab(path.as_uri()):
                    raise RuntimeError("No external viewer accepted the immutable temporary copy")
                message = f"Opened original copy: {path.as_uri()} (removed when this TUI closes)"
            except InterruptedError:
                return
            except Exception as exc:
                if path is not None:
                    path.unlink(missing_ok=True)
                message = f"External open failed: {exc}; source remains artifact://{sha256}"
            self.event_queue.put(events.ArtifactOpenEvent(**context, request_id=request_id, message=message))

        self._work.submit("ArtifactOpen", worker)
