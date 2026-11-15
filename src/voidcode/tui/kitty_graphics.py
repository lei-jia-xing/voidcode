"""Trusted Kitty Graphics commands and correlated terminal replies."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass

ST = b"\x1b\\"
ESC = b"\x1b"
KITTY_PROBE_ID = 0xFFFFFFFE
_MAX_ID = 0xFFFFFFFF
_RAW_CHUNK_BYTES = 3 * 1024


@dataclass(frozen=True, slots=True)
class KittyGraphicsReply:
    image_id: int | None
    placement_id: int | None
    message: str


def probe_sequence() -> bytes:
    """Query Kitty Graphics protocol support without creating stored pixels."""
    return b"\x1b_Gi=" + str(KITTY_PROBE_ID).encode("ascii") + b",s=1,v=1,a=q,t=d,f=24;AAAA" + ST


def parse_reply(sequence: bytes) -> KittyGraphicsReply | None:
    if not sequence.startswith(b"\x1b_G") or not sequence.endswith(ST):
        return None
    try:
        header, payload = sequence[3:-2].split(b";", 1)
    except ValueError:
        return None
    fields: dict[bytes, bytes] = {}
    for item in header.split(b","):
        key, separator, value = item.partition(b"=")
        if separator:
            fields[key] = value
    raw_id = fields.get(b"i")
    raw_placement = fields.get(b"p")
    try:
        image_id = int(raw_id) if raw_id is not None else None
        placement_id = int(raw_placement) if raw_placement is not None else None
    except ValueError:
        return None
    if image_id is not None and not 1 <= image_id <= _MAX_ID:
        return None
    if placement_id is not None and not 1 <= placement_id <= _MAX_ID:
        return None
    return KittyGraphicsReply(image_id, placement_id, payload.decode("ascii", errors="backslashreplace"))


def transmit_png(image_id: int, png: bytes) -> Iterator[bytes]:
    """Transmit stored PNG bytes in bounded APC payloads and request success/error ack."""
    if not 1 <= image_id <= _MAX_ID:
        raise ValueError("Kitty image id must be a positive 32-bit integer")
    if not png:
        raise ValueError("Kitty PNG data must not be empty")
    for start in range(0, len(png), _RAW_CHUNK_BYTES):
        chunk = png[start : start + _RAW_CHUNK_BYTES]
        more = start + len(chunk) < len(png)
        fields = f"a=t,f=100,i={image_id},m={int(more)},q=0" if start == 0 else f"m={int(more)}"
        yield ESC + b"_G" + fields.encode("ascii") + b";" + base64.b64encode(chunk) + ST


def place_image(image_id: int, placement_id: int, columns: int) -> bytes:
    """Place a cached PNG at the current cursor; Kitty computes aspect-preserving rows."""
    if not 1 <= image_id <= _MAX_ID or not 1 <= placement_id <= _MAX_ID or columns <= 0:
        raise ValueError("invalid Kitty image placement")
    return f"\x1b_Ga=p,i={image_id},p={placement_id},c={columns},C=1,q=1;\x1b\\".encode("ascii")


def delete_placement(image_id: int, placement_id: int) -> bytes:
    if not 1 <= image_id <= _MAX_ID or not 1 <= placement_id <= _MAX_ID:
        raise ValueError("invalid Kitty image placement")
    return f"\x1b_Ga=d,d=i,i={image_id},p={placement_id},q=1;\x1b\\".encode("ascii")


def delete_image(image_id: int) -> bytes:
    if not 1 <= image_id <= _MAX_ID:
        raise ValueError("invalid Kitty image id")
    return f"\x1b_Ga=d,d=I,i={image_id},q=1;\x1b\\".encode("ascii")
