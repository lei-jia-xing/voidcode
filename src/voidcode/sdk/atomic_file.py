"""Atomic sibling-file replacement shared by product config and file tools."""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path


def replace_file(path: Path, content: bytes, *, expected_content: bytes | None) -> None:
    """Commit file bytes without exposing a partially written result."""
    mode = 0o644 if expected_content is None else path.stat().st_mode
    descriptor, temporary_name = tempfile.mkstemp(prefix=".voidcode_edit_", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        # Detect changes since validation. Like write's replace boundary, this
        # is a best-effort check, not a cross-process compare-and-swap.
        if expected_content is None:
            # Linking a complete sibling creates the name atomically and fails
            # if another writer created it after preflight.
            os.link(temporary, path)
        else:
            if path.is_symlink() or path.read_bytes() != expected_content:
                raise OSError(errno.ESTALE, "File changed since edit validation", str(path))
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
