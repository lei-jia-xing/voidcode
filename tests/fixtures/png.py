"""Deterministic RGB PNGs with real compressed scanlines and verified chunk CRCs."""

from __future__ import annotations

import random
import struct
import zlib


def rgb_png(width: int = 384, height: int = 300) -> bytes:
    pixels = random.Random(731).randbytes(width * height * 3)
    scanlines = b"".join(b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3] for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def decode_rgb_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode this fixture's standard RGB/filter-0 PNG representation, checking every chunk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    offset = 8
    compressed = bytearray()
    dimensions: tuple[int, int] | None = None
    ended = False
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        if zlib.crc32(kind + body) != crc:
            raise ValueError("PNG chunk CRC mismatch")
        offset += length + 12
        if kind == b"IHDR":
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, colour, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ValueError("unexpected PNG representation")
            dimensions = width, height
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"IEND":
            ended = True
            break
    if dimensions is None or not ended or offset != len(data):
        raise ValueError("incomplete PNG")
    width, height = dimensions
    decoded = zlib.decompress(compressed)
    stride = width * 3 + 1
    if len(decoded) != stride * height or any(decoded[row * stride] != 0 for row in range(height)):
        raise ValueError("invalid PNG scanlines")
    pixels = b"".join(decoded[row * stride + 1 : (row + 1) * stride] for row in range(height))
    return width, height, pixels
