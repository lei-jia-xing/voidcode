"""Clipboard image ingress for the TUI.

The client uses the desktop's existing ``wl-paste``/``xclip`` command when
present and never imports a Python clipboard package. Only declared image
targets are read; plain text is never reinterpreted as a local file path. A
file-manager ``text/uri-list`` entry is accepted only when it names exactly one
local file, and that file still goes through the same bounded image validator.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlparse

from .attachments import PendingImage, load_image_file, pending_image_from_bytes

_CLIPBOARD_TIMEOUT_SECONDS: Final[float] = 2.0
_IMAGE_TARGETS: Final[tuple[str, ...]] = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")
_URI_TARGET: Final[str] = "text/uri-list"


class ClipboardImageUnavailable(RuntimeError):
    """The clipboard cannot provide image bytes or one unambiguous image path."""


@dataclass(frozen=True, slots=True)
class _Backend:
    tool: str
    wayland: bool

    def targets_argv(self) -> tuple[str, ...]:
        if self.wayland:
            return (self.tool, "--list-types")
        return (self.tool, "-selection", "clipboard", "-t", "TARGETS", "-o")

    def read_argv(self, target: str) -> tuple[str, ...]:
        if self.wayland:
            return (self.tool, "--no-newline", "--type", target)
        return (self.tool, "-selection", "clipboard", "-t", target, "-o")


def _command(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, capture_output=True, timeout=_CLIPBOARD_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ClipboardImageUnavailable(f"{argv[0]} timed out reading the clipboard") from exc
    except FileNotFoundError as exc:
        raise ClipboardImageUnavailable(f"{argv[0]} is not installed") from exc
    except OSError as exc:
        raise ClipboardImageUnavailable(f"cannot run {argv[0]}: {exc}") from exc


def _backends() -> tuple[_Backend, ...]:
    backends: list[_Backend] = []
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        backends.append(_Backend("wl-paste", wayland=True))
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        backends.append(_Backend("xclip", wayland=False))
    return tuple(backends)


def _targets(backend: _Backend) -> tuple[str, ...]:
    result = _command(backend.targets_argv())
    if result.returncode != 0:
        return ()
    return tuple(line.strip().lower() for line in result.stdout.decode("utf-8", errors="replace").splitlines() if line.strip())


def _read_target(backend: _Backend, target: str) -> bytes:
    result = _command(backend.read_argv(target))
    if result.returncode != 0 or not result.stdout:
        raise ClipboardImageUnavailable(f"{backend.tool} exposed no {target} data")
    return result.stdout


def _single_image_path(uri_list: str) -> str:
    entries = [line.strip() for line in uri_list.splitlines() if line.strip() and not line.startswith("#")]
    if len(entries) != 1:
        raise ClipboardImageUnavailable(f"clipboard holds {len(entries)} file entries; attach local images explicitly with /tui attach <path>")
    parsed = urlparse(entries[0])
    if parsed.scheme != "file":
        raise ClipboardImageUnavailable("clipboard URI is not a local file")
    if parsed.netloc not in ("", "localhost"):
        raise ClipboardImageUnavailable("clipboard URI names a remote file")
    path = unquote(parsed.path)
    if not path:
        raise ClipboardImageUnavailable("clipboard URI has no local path")
    return str(Path(path))


def read_clipboard_image() -> PendingImage:
    """Read one image from the desktop clipboard or fail with a typed reason."""
    backends = _backends()
    if not backends:
        raise ClipboardImageUnavailable("no supported clipboard reader found (wl-paste or xclip); text is never read as an image path")
    failures: list[str] = []
    for backend in backends:
        try:
            targets = _targets(backend)
        except ClipboardImageUnavailable as exc:
            failures.append(str(exc))
            continue
        image_target = next((target for target in _IMAGE_TARGETS if target in targets), None)
        if image_target is not None:
            return pending_image_from_bytes(_read_target(backend, image_target), image_target, "clipboard image")
        if _URI_TARGET in targets:
            try:
                path = _single_image_path(_read_target(backend, _URI_TARGET).decode("utf-8", errors="replace"))
                return load_image_file(path)
            except ClipboardImageUnavailable as exc:
                failures.append(str(exc))
                continue
        failures.append(f"{backend.tool} exposes no image target")
    raise ClipboardImageUnavailable("; ".join(failures) or "clipboard holds no image data")


__all__ = ["ClipboardImageUnavailable", "read_clipboard_image"]
