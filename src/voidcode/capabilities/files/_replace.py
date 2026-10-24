"""Replace an existing file without exposing a partially written result."""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path


def replace_file(path: Path, content: bytes, *, expected_content: bytes) -> None:
    mode = path.stat().st_mode
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
        if path.is_symlink() or path.read_bytes() != expected_content:
            raise OSError(errno.ESTALE, "File changed since edit validation", str(path))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
