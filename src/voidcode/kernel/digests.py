"""Source-identity digests: one framing rule for every tree a version pin names.

A digest that concatenated raw file bytes could be satisfied by two different trees - one file's
content can embed what looks like another file's path and bytes - so every file contributes its
relative path, a separator, and a *fixed-size* content digest. Files are hashed as streams rather
than read whole into memory, and each caller states which files it covers: an installation image
covers everything it serves, an SDK pin covers its Python sources.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "IGNORED_DIRECTORIES",
    "framed_files_digest",
    "package_files",
    "package_tree_digest",
    "python_tree_digest",
]

#: Directories that hold no source identity: caches never change what a tree means.
IGNORED_DIRECTORIES = frozenset({"__pycache__"})


def package_files(root: Path) -> tuple[Path, ...]:
    """Every file one installation carries, caches excluded, in a stable order.

    Cache directories are ignored relative to the hashed root, so a tree that merely *lives under* a
    directory called ``__pycache__`` still hashes all of its own files.
    """
    resolved = Path(root).resolve(strict=True)
    return tuple(
        sorted(path for path in resolved.rglob("*") if path.is_file() and not IGNORED_DIRECTORIES.intersection(path.relative_to(resolved).parts))
    )


def package_tree_digest(root: Path) -> str:
    """Identity of one installation tree, including the assets it serves."""
    resolved = Path(root).resolve(strict=True)
    return _framed_digest(resolved, package_files(resolved))


def python_tree_digest(root: Path) -> str:
    """Identity of one Python source tree: what an SDK or protocol pin needs."""
    resolved = Path(root).resolve(strict=True)
    sources = (path for path in resolved.rglob("*.py") if path.is_file() and not IGNORED_DIRECTORIES.intersection(path.relative_to(resolved).parts))
    return _framed_digest(resolved, sources)


def framed_files_digest(entries: Iterable[tuple[str, Path]]) -> str:
    """One framed digest over explicitly named files: the same rule `_framed_digest` applies.

    Packaging layouts serve some bytes from outside the hashed tree (a source checkout serves its
    freshly built web bundle); naming those files under the path the image stages them at keeps one
    digest covering what the process actually runs.
    """
    digest = hashlib.sha256()
    for name, path in sorted(entries):
        digest.update(name.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def _framed_digest(root: Path, files: Iterable[Path]) -> str:
    return framed_files_digest([(path.relative_to(root).as_posix(), path) for path in files])
