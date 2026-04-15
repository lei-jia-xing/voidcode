from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

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

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=200)

_path_chars = st.characters(
    min_codepoint=33,
    max_codepoint=126,
    blacklist_characters=['"', "\n", "\r", "\t", "/"],
)
_path_segment = st.text(alphabet=_path_chars, min_size=1, max_size=8)
_slash_path = st.lists(_path_segment, min_size=1, max_size=3).map("/".join)
_space_path = st.lists(_path_segment, min_size=1, max_size=2).map(" ".join)
_relative_path = st.one_of(_slash_path, _space_path)
_line_chars = st.characters(
    min_codepoint=32,
    max_codepoint=126,
    blacklist_characters=["\n", "\r"],
)
_line_text = st.text(alphabet=_line_chars, min_size=0, max_size=12)


@CI_SETTINGS
@given(old_path=_relative_path, new_path=_relative_path)
def test_parse_diff_git_paths_round_trips_supported_headers(old_path: str, new_path: str) -> None:
    header = _format_diff_git_line(old_path, new_path)

    assert _parse_diff_git_paths(header) == (old_path, new_path)


@CI_SETTINGS
@given(
    old_path=_relative_path,
    new_path=_relative_path,
    old_mode=st.sampled_from(("100644", "100755")),
    new_mode=st.sampled_from(("100644", "100755")),
)
def test_normalize_patch_text_is_idempotent_for_mode_only_blocks(
    old_path: str,
    new_path: str,
    old_mode: str,
    new_mode: str,
) -> None:
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


@CI_SETTINGS
@given(
    old_path=_relative_path,
    new_path=_relative_path,
    old_mode=st.sampled_from(("100644", "100755")),
    new_mode=st.sampled_from(("100644", "100755")),
    before_line=_line_text,
    after_line=_line_text,
)
def test_mode_only_detection_stays_false_once_patch_has_markers_or_hunks(
    old_path: str,
    new_path: str,
    old_mode: str,
    new_mode: str,
    before_line: str,
    after_line: str,
) -> None:
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


@CI_SETTINGS
@given(path=_relative_path)
def test_changes_from_patch_dedupes_mode_only_metadata_entries(path: str) -> None:
    patch_text = "\n".join(
        [
            _format_diff_git_line(path, path),
            "old mode 100644",
            "new mode 100755",
            "",
        ]
    )

    assert _changes_from_patch(patch_text) == [{"path": path, "status": "M"}]


@CI_SETTINGS
@given(paths=st.tuples(_relative_path, _relative_path).filter(lambda pair: pair[0] != pair[1]))
def test_changes_from_patch_preserves_rename_metadata(paths: tuple[str, str]) -> None:
    old_path, new_path = paths

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
