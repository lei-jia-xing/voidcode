"""Kitty Graphics capability probing, uploads, placements, and reply routing."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from .kitty_graphics import KITTY_PROBE_ID, KittyGraphicsReply, parse_reply, probe_sequence, transmit_png
from .term_capabilities import TerminalCapabilityError

__all__ = ["KittyGraphicsControl", "TerminalKittyReply"]


@dataclass(frozen=True, slots=True)
class TerminalKittyReply:
    sha256: str
    operation: str
    message: str


class KittyGraphicsControl:
    """Terminal mixin owning Kitty protocol state; the terminal supplies I/O."""

    _is_tty: bool
    _input_fd: int | None
    _eof: bool
    _lock: threading.RLock
    _pending_input: bytearray
    _kitty_supported: bool
    _kitty_probe_pending: bool
    _kitty_probed: bool
    _kitty_input: bytearray
    _kitty_replies: list[TerminalKittyReply]
    _kitty_uploads: dict[int, tuple[str, tuple[int, int]]]
    _kitty_pending_placements: dict[tuple[int, int], str]
    _kitty_images: dict[str, tuple[int, tuple[int, int]]]
    _kitty_image_ids: dict[int, str]
    _kitty_placements: dict[str, int]

    def _emit(self, text: str) -> None:
        raise NotImplementedError

    def _read_input_chunk(self, timeout: float | None) -> bytes | None:
        raise NotImplementedError

    @property
    def kitty_graphics_supported(self) -> bool:
        self._ensure_kitty_probed()
        return self._kitty_supported

    def _ensure_kitty_probed(self) -> None:
        """Run the one graphics-capability probe, on first image render instead of at startup."""
        if self._kitty_probed:
            return
        with self._lock:
            if self._kitty_probed:
                return
            self._kitty_probed = True
            self._probe_kitty_graphics()

    def cached_kitty_image(self, sha256: str) -> tuple[int, int] | None:
        cached = self._kitty_images.get(sha256)
        return cached[1] if cached is not None else None

    def kitty_placement_id(self, sha256: str) -> int | None:
        if sha256 not in self._kitty_images:
            return None
        placement = self._kitty_placements.get(sha256)
        if placement is None:
            placement = self._new_kitty_id()
            self._kitty_placements[sha256] = placement
        return placement

    def upload_kitty_png(self, sha256: str, png: bytes, dimensions: tuple[int, int]) -> int:
        """Transmit trusted PNG bytes once; the correlated terminal reply validates decoding."""
        self._ensure_kitty_probed()
        if not self._kitty_supported:
            raise TerminalCapabilityError("Kitty Graphics is not supported by the active terminal")
        cached = self._kitty_images.get(sha256)
        if cached is not None:
            return cached[0]
        for image_id, (pending_sha, _) in self._kitty_uploads.items():
            if pending_sha == sha256:
                return image_id
        image_id = self._new_kitty_id()
        self._kitty_uploads[image_id] = (sha256, dimensions)
        self._kitty_image_ids[image_id] = sha256
        try:
            with self._lock:
                for sequence in transmit_png(image_id, png):
                    self._emit(sequence.decode("ascii"))
        except BaseException:
            self._kitty_uploads.pop(image_id, None)
            self._kitty_image_ids.pop(image_id, None)
            raise
        if self._eof:
            self._kitty_uploads.pop(image_id, None)
            self._kitty_image_ids.pop(image_id, None)
            raise TerminalCapabilityError("terminal disconnected during Kitty image upload")
        return image_id

    def drain_kitty_replies(self) -> tuple[TerminalKittyReply, ...]:
        replies = tuple(self._kitty_replies)
        self._kitty_replies.clear()
        return replies

    def _probe_kitty_graphics(self) -> None:
        if not self._is_tty or self._input_fd is None:
            return
        self._kitty_probe_pending = True
        self._emit(probe_sequence().decode("ascii"))
        deadline = time.monotonic() + 0.12
        while self._kitty_probe_pending and time.monotonic() < deadline:
            chunk = self._read_input_chunk(max(0.0, deadline - time.monotonic()))
            if chunk is None:
                break
            if not chunk:
                self._eof = True
                break
            self._pending_input.extend(self._filter_kitty_replies(chunk))

    def _filter_kitty_replies(self, data: bytes) -> bytes:
        self._kitty_input.extend(data)
        passthrough = bytearray()
        marker = b"\x1b_G"
        while self._kitty_input:
            start = self._kitty_input.find(marker)
            if start < 0:
                keep = 0
                for length in range(1, len(marker)):
                    if self._kitty_input.endswith(marker[:length]):
                        keep = length
                passthrough.extend(self._kitty_input[:-keep] if keep else self._kitty_input)
                if keep:
                    del self._kitty_input[:-keep]
                else:
                    self._kitty_input.clear()
                break
            if start:
                passthrough.extend(self._kitty_input[:start])
                del self._kitty_input[:start]
            end = self._kitty_input.find(b"\x1b\\", len(marker))
            if end < 0:
                break
            sequence = bytes(self._kitty_input[: end + 2])
            del self._kitty_input[: end + 2]
            reply = parse_reply(sequence)
            if reply is None or not self._route_kitty_reply(reply):
                passthrough.extend(sequence)
        return bytes(passthrough)

    def _route_kitty_reply(self, reply: KittyGraphicsReply) -> bool:
        image_id = reply.image_id
        message = reply.message
        if image_id == KITTY_PROBE_ID and self._kitty_probe_pending:
            self._kitty_supported = message == "OK"
            self._kitty_probe_pending = False
            return True
        if image_id is None:
            return False
        upload = self._kitty_uploads.pop(image_id, None)
        if upload is not None:
            sha256, dimensions = upload
            if message == "OK":
                self._kitty_images[sha256] = (image_id, dimensions)
                self._kitty_replies.append(TerminalKittyReply(sha256, "upload", message))
            else:
                self._kitty_image_ids.pop(image_id, None)
                self._kitty_replies.append(TerminalKittyReply(sha256, "upload", message))
            return True
        placement_id = reply.placement_id
        if placement_id is not None:
            sha256 = self._kitty_pending_placements.pop((image_id, placement_id), None)
            if sha256 is not None:
                if message != "OK":
                    self._kitty_images.pop(sha256, None)
                    self._kitty_image_ids.pop(image_id, None)
                self._kitty_replies.append(TerminalKittyReply(sha256, "placement", message))
                return True
        return image_id in self._kitty_image_ids

    def _new_kitty_id(self) -> int:
        used = set(self._kitty_image_ids) | set(self._kitty_placements.values()) | {KITTY_PROBE_ID}
        while True:
            value = secrets.randbelow(0xFFFFFFFF - 1) + 1
            if value not in used:
                return value
