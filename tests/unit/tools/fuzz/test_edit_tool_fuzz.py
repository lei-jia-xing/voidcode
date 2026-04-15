from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_edit = importlib.import_module("voidcode.tools.edit")
_normalize_line_endings = cast(Callable[[str], str], _edit._normalize_line_endings)
_detect_line_ending = cast(Callable[[str], str], _edit._detect_line_ending)
_convert_line_endings = cast(Callable[[str, str], str], _edit._convert_line_endings)
_trim_diff = cast(Callable[[str], str], _edit._trim_diff)


class _ReplaceFn(Protocol):
    def __call__(
        self, content: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> tuple[str, int]: ...


_replace = cast(_ReplaceFn, _edit._replace)

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=200)

_text_chars = st.characters(blacklist_characters=["\x00"])
_single_line = st.text(alphabet=_text_chars, min_size=0, max_size=20).filter(
    lambda text: "\n" not in text and "\r" not in text
)
_multiline_text = st.lists(_single_line, min_size=0, max_size=6).map("\n".join)
_search_line = st.text(
    alphabet=st.characters(blacklist_characters=["\x00", "\n", "\r"]),
    min_size=1,
    max_size=8,
)
_replacement_line = st.text(
    alphabet=st.characters(blacklist_characters=["\x00", "\n", "\r"]),
    min_size=0,
    max_size=8,
)


@CI_SETTINGS
@given(text=st.text(alphabet=_text_chars, min_size=0, max_size=80))
def test_normalize_line_endings_is_idempotent(text: str) -> None:
    normalized = _normalize_line_endings(text)

    assert _normalize_line_endings(normalized) == normalized


@CI_SETTINGS
@given(lines=st.lists(_single_line, min_size=0, max_size=6), ending=st.sampled_from(("\n", "\r\n")))
def test_detect_and_convert_line_endings_round_trip(lines: list[str], ending: str) -> None:
    text = ending.join(lines)

    converted = _convert_line_endings(_normalize_line_endings(text), ending)

    if ending == "\r\n" and len(lines) > 1:
        assert _detect_line_ending(converted) == ending
    else:
        assert _detect_line_ending(converted) == "\n"
    assert _normalize_line_endings(converted) == _normalize_line_endings(text)


@CI_SETTINGS
@given(diff_body=_multiline_text)
def test_trim_diff_is_idempotent(diff_body: str) -> None:
    trimmed = _trim_diff(diff_body)

    assert _trim_diff(trimmed) == trimmed


@CI_SETTINGS
@given(
    content=st.text(alphabet=st.characters(blacklist_characters=["\x00"]), min_size=1, max_size=40)
)
def test_replace_all_replacing_content_with_itself_is_rejected(content: str) -> None:
    old = "X"
    with pytest.raises(ValueError, match="identical"):
        _replace(content, old, old, replace_all=True)


@CI_SETTINGS
@given(
    prefix=_single_line.filter(lambda text: "UNIQUE_MARKER" not in text),
    suffix=_single_line.filter(lambda text: "UNIQUE_MARKER" not in text),
    replacement=_replacement_line.filter(lambda text: text not in {"", "UNIQUE_MARKER"}),
)
def test_replace_single_occurrence_updates_exactly_one_unique_match(
    prefix: str, suffix: str, replacement: str
) -> None:
    old = "UNIQUE_MARKER"
    content = f"{prefix}{old}{suffix}"
    if replacement == old:
        replacement = f"{replacement}_changed"

    updated, count = _replace(content, old, replacement, replace_all=False)

    assert count == 1
    assert updated.count(old) == 0
    assert len(updated) == len(content) - len(old) + len(replacement)
    assert replacement in updated
