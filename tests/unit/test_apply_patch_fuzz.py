from __future__ import annotations

import importlib
from collections.abc import Callable
from random import Random
from typing import cast

_apply_patch = importlib.import_module("voidcode.tools.apply_patch")
_changes_from_patch = cast(
    Callable[[str], list[dict[str, object]]], _apply_patch._changes_from_patch
)
_format_diff_git_line = cast(Callable[[str, str], str], _apply_patch._format_diff_git_line)
_looks_like_mode_only_patch = cast(Callable[[str], bool], _apply_patch._looks_like_mode_only_patch)
_normalize_patch_text = cast(Callable[[str], str], _apply_patch._normalize_patch_text)
_parse_diff_git_paths = cast(
    Callable[[str], tuple[str, str] | None], _apply_patch._parse_diff_git_paths
)


def _random_path(rng: Random) -> str:
    segment_count = rng.randint(1, 3)
    chars = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    spaced = rng.random() < 0.3
    separator = " " if spaced else "/"
    segments: list[str] = []
    for _ in range(segment_count):
        length = rng.randint(1, 8)
        segments.append("".join(rng.choice(chars) for _ in range(length)))
    return separator.join(segments)


def _random_line(rng: Random) -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789 _-"
    length = rng.randint(0, 12)
    return "".join(rng.choice(chars) for _ in range(length))


def test_parse_diff_git_paths_round_trips_supported_headers() -> None:
    rng = Random(20260415)

    for _ in range(200):
        old_path = _random_path(rng)
        new_path = _random_path(rng)
        header = _format_diff_git_line(old_path, new_path)

        assert _parse_diff_git_paths(header) == (old_path, new_path)


def test_normalize_patch_text_is_idempotent_for_mode_only_blocks() -> None:
    rng = Random(20260416)

    for _ in range(200):
        old_path = _random_path(rng)
        new_path = _random_path(rng)
        old_mode = rng.choice(("100644", "100755"))
        new_mode = rng.choice(("100644", "100755"))
        patch_text = "\n".join(
            [
                _format_diff_git_line(old_path, new_path),
                f"old mode {old_mode}",
                f"new mode {new_mode}",
                "",
            ]
        )

        normalized_once = _normalize_patch_text(patch_text)
        normalized_twice = _normalize_patch_text(normalized_once)

        assert normalized_once == normalized_twice


def test_mode_only_detection_stays_false_once_patch_has_markers_or_hunks() -> None:
    rng = Random(20260417)

    for _ in range(200):
        old_path = _random_path(rng)
        new_path = _random_path(rng)
        old_mode = rng.choice(("100644", "100755"))
        new_mode = rng.choice(("100644", "100755"))
        before_line = _random_line(rng)
        after_line = _random_line(rng)
        patch_text = "\n".join(
            [
                _format_diff_git_line(old_path, new_path),
                f"old mode {old_mode}",
                f"new mode {new_mode}",
                f"--- a/{old_path}",
                f"+++ b/{new_path}",
                "@@ -1 +1 @@",
                f"-{before_line}",
                f"+{after_line}",
                "",
            ]
        )

        assert _looks_like_mode_only_patch(patch_text) is False


def test_changes_from_patch_dedupes_mode_only_metadata_entries() -> None:
    rng = Random(20260418)

    for _ in range(200):
        path = _random_path(rng)
        patch_text = "\n".join(
            [
                _format_diff_git_line(path, path),
                "old mode 100644",
                "new mode 100755",
                "",
            ]
        )

        assert _changes_from_patch(patch_text) == [{"path": path, "status": "M"}]


def test_changes_from_patch_preserves_rename_metadata() -> None:
    rng = Random(20260419)

    for _ in range(200):
        old_path = _random_path(rng)
        new_path = _random_path(rng)
        patch_text = "\n".join(
            [
                _format_diff_git_line(old_path, new_path),
                "similarity index 100%",
                f"rename from {old_path}",
                f"rename to {new_path}",
                "",
            ]
        )

        assert _changes_from_patch(patch_text) == [
            {"path": new_path, "old_path": old_path, "status": "R"}
        ]
