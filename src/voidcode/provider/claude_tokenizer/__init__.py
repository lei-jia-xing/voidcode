"""Offline reproduction of Claude's ``count_tokens``, aligned with oh-my-pi.

oh-my-pi counts the four Claude encodings with a port of
`sanderland/ctok <https://github.com/sanderland/ctok>`_ (MIT), pinned upstream
at revision ``df3b59b``. This package is that same reconstruction, vendored so
voidcode reaches the identical numbers with no native addon and no network.

Per message, exactly as upstream and omp run it:

1. normalize (NFC; C0/C1 and BMP-private-use stripping; NUL and exotic-space
   folding; Thai SARA AM composition; and -- v3 only -- curly-quote folding);
2. rewrite into a marked stream: words are bracketed by ``⟨bow⟩``/``⟨eow⟩`` and
   case-normalized, a single space between two marked spans is the
   ``⟨eow⟩⟨bow⟩`` seam, digits and punctuation take their measured border
   markers;
3. min-cost tile that stream over the vocabulary plus a guaranteed
   one-character floor, where an uncovered character falls to a UTF-8 byte
   tiling over the container's byte-prefix table;
4. add the family's measured message frame.

The vocabulary is the ``CTOK\\x02`` container the addon ships (extracted by
``scripts/extract_tokenizer_data.py``). It is not a BPE merge table: it is
ctok's tiling vocabulary written in the byte spelling ``01`` ``02`` ``04``
``05`` for ``⟨bow⟩`` ``⟨eow⟩`` ``⟨shift⟩`` ``⟨caps⟩``, front-coded and
bytewise sorted. Because the markers are single bytes rather than ctok's
noncharacter codepoints, the front-coding is three bytes shorter per
occurrence -- the form omp's port matches.

Counts are verified exact against the native oracle: 0 mismatches on 6000
realistic source chunks, 109 adversarial strings, and a 241 KB mixed
English/Chinese/Japanese/code document, for all four encodings.
"""

from __future__ import annotations

import bz2
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Final

from .engine import VocabCore, content_token_count

_DATA_PACKAGE: Final = "voidcode.provider.tokenizer_data"


@dataclass(frozen=True, slots=True)
class _Family:
    """One encoding's vocabulary file plus the frame omp gives it."""

    vocab_file: str
    message_overhead: int
    fold_quotes: bool
    allcaps_min: int | None
    frame_bow: bool
    frame_tail: str
    appended_newlines: int


#: The four Claude encodings. ``ClaudeV47``/``ClaudeV5``/``ClaudeV5Sonnet``
#: share the v4.7 vocabulary and differ only in the message frame, exactly as
#: omp's ``Family::params`` overrides it; ``ClaudeV3`` is the v3 vocabulary.
_FAMILIES: Final[dict[str, _Family]] = {
    "ClaudeV3": _Family("ctok_v3.bin.bz2", 7, True, 4, True, "ladder", 2),
    "ClaudeV47": _Family("ctok_v4_7.bin.bz2", 11, False, None, True, "ladder", 2),
    "ClaudeV5": _Family("ctok_v4_7.bin.bz2", 6, False, None, False, "free", 0),
    # Measured live against claude-sonnet-5: no frame ⟨bow⟩, trailing ASCII
    # whitespace absorbed, and the frame appends nothing to the newline ladder.
    "ClaudeV5Sonnet": _Family("ctok_v4_7.bin.bz2", 6, False, None, False, "ladder", 0),
}


@cache
def _core(name: str) -> VocabCore:
    """Load and cache one family's vocabulary. Importing this module reads nothing."""
    family = _FAMILIES[name]
    blob = bz2.decompress(files(_DATA_PACKAGE).joinpath(family.vocab_file).read_bytes())
    return VocabCore.parse(
        blob,
        message_overhead=family.message_overhead,
        fold_quotes=family.fold_quotes,
        allcaps_min=family.allcaps_min,
        frame_bow=family.frame_bow,
        frame_tail=family.frame_tail,
        appended_newlines=family.appended_newlines,
    )


def count_tokens(text: str, encoding: str) -> int:
    """Claude's ``count_tokens`` for one message under ``encoding``.

    ``encoding`` is an omp ``Encoding`` member (``ClaudeV47``); the caller folds
    the catalog's kebab-case spellings first.

    ``count_tokens("")`` is a documented divergence: the native addon returns 1
    for v3/v4.7 and 0 for the v5-series, but the shared seam in
    :mod:`.provider.tokenizer` returns 0 for empty text on every encoding, and
    callers already skip empty payloads. Every non-empty input is exact.
    """
    if not text:
        # The v5-series frame absorbs trailing whitespace and appends no
        # newline, so an empty message costs zero there; v3/v4.7 still pay for
        # the frame's own ⟨bow⟩.
        return 0 if not _FAMILIES[encoding].frame_bow else 1
    return content_token_count(text, _core(encoding))


def encodings() -> tuple[str, ...]:
    """Claude encodings this module can count exactly."""
    return tuple(sorted(_FAMILIES))


__all__ = ["count_tokens", "encodings"]
