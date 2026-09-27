"""Per-model token counting, aligned with oh-my-pi's tokenizer ladder.

omp's ``countTokens`` resolves, in order:

1. the model's catalog entry names a ``tokenizer`` -> the real native tokenizer
   (a Rust BPE over that encoding's vocabulary);
2. otherwise -> ``(utf8_bytes + 3) >> 2``, a byte-count guess;
3. ``PI_TOKENIZER_ACCURATE=1`` -> force a real tokenizer instead of (2).

voidcode mirrors (1) and (2). Every number this module returns is an *estimate*
of what a provider will charge; only a model with a known tokenizer gets a real
count, and the byte guess is what omp itself uses for everything else.

The vocabularies are the ones shipped by oh-my-pi's native addon, extracted by
``scripts/extract_tokenizer_data.py`` and stored bz2-compressed under
``tokenizer_data/``. They are loaded lazily: importing this module reads no
vocabulary, and a process that never counts a token never pays for one.

No network access is ever performed: encodings are constructed directly from
the shipped ``mergeable_ranks``, which bypasses ``tiktoken.get_encoding`` (the
only path that downloads vocabulary data).
"""

from __future__ import annotations

import bz2
import unicodedata
from functools import cache
from importlib.resources import files
from typing import Final, Literal

import tiktoken

from .claude_tokenizer import count_tokens as _claude_count_tokens
from .claude_tokenizer import encodings as _claude_encodings

_DATA_PACKAGE: Final = "voidcode.provider.tokenizer_data"

#: Claude encodings, served by the vendored ctok engine instead of a tiktoken
#: vocabulary. Held separately because that engine owns normalization and
#: splitting, so the table's own splitter/normalization columns stay empty.
_CLAUDE: Final[frozenset[str]] = frozenset(_claude_encodings())

#: Pretokenizer patterns, recovered behaviourally from omp's hand-written Rust
#: splitter. ``CL100K``/``O200K`` are tiktoken's own published patterns and were
#: reproduced exactly; the other four were recovered by differential search
#: against the native addon (see the generator's module docstring).
_PAT_CL100K: Final = r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
_PAT_O200K: Final = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)
#: omp's KimiK2 splitter is the o200k word rule with the punctuation class
#: narrowed to ``[\r\n]`` (no ``/``).
_PAT_O200K_NO_SLASH: Final = _PAT_O200K.replace(r"[\r\n/]*", r"[\r\n]*")
#: Qwen3 additionally NFC-normalizes its input (verified against the addon:
#: ``native("e\u0301")`` counts the precomposed ``é``). The punctuation-run
#: class excludes ``\p{M}``, matching upstream's
#: `` ?[^\s\p{L}\p{M}\p{N}]+``: without that exclusion a combining mark or
#: variation selector (``\ufe0f``) that follows punctuation was absorbed into
#: the punctuation piece instead of standing alone, e.g. ZWJ + U+2764 + VS16
#: counted 3 instead of the addon's 4.
_PAT_QWEN3: Final = r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?[\p{L}\p{M}]+|\p{N}{1,3}+| ?[^\s\p{L}\p{M}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
#: DeepSeekV3 splits in three chained ``Split(Isolated)`` stages, not one
#: regex — upstream's own ``tokenizer.json`` (``deepseek-ai/DeepSeek-V3``,
#: ``pre_tokenizer``) and omp's hand-written port of it
#: (``crates/pi-natives/src/utok/scan/deepseek.rs``) agree exactly:
#:
#: 1. ``\p{N}{1,3}``            — digit groups, cut first
#: 2. ``[\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff]+`` — Han + kana runs
#: 3. ``[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~][A-Za-z]+``
#:    ``|[^\r\n\p{L}\p{P}\p{S}]?[\p{L}\p{M}]+``
#:    ``| ?[\p{P}\p{S}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+``
#:
#: `tiktoken` takes one ``pat_str``, so the three stages are folded into the
#: alternation below — equivalent piece-for-piece, not merely in aggregate.
#: Three things a literal transcription gets wrong:
#:
#: * Stage 3 runs per piece, so a match may never cross a Han/kana codepoint
#:   (``\p{L}``/``\p{P}``/``\p{S}`` are unbounded there in the raw regex). The
#:   stage-3 classes are CJK-bounded by set intersection, and Han/kana
#:   codepoints get leading alternates: Han/kana letters stay a run, Han/kana
#:   punctuation and symbols (``\u30a0``, ``\u30fb``, ...) take the punctuation
#:   run without its ``[\r\n]*`` tail, since the piece is already CJK-bounded.
#: * Stage 3 has no alternate for gap codepoints (an isolated digit after
#:   stage 1, an unassigned slot, a format character), and ``Isolated`` keeps
#:   them as pieces; `tiktoken`'s ``findall`` silently *drops* unmatched text,
#:   so the gap runs are spelled out. Each run stops where a gap codepoint is
#:   itself prefix-eligible and a letter follows, because that codepoint opens
#:   a stage-3 letter match (``"\u200c\u200b" + "p"`` is gap ``\u200c``, then
#:   the prefix piece ``\u200bp``).
#: * The scanner's ``ws_end`` returns the whole run when it reaches a
#:   stage-1/2 boundary, which the plain ``\s+(?!\S)`` rule does not: ``"  1"``
#:   is the stage-1 piece ``"  "`` plus ``"1"`` while ``"  一"`` is ``"  "``
#:   plus ``"一"``, whereas ``\s+(?!\S)`` alone would split them one space in.
#:   The ``\s+(?=[\p{N}...]|(?![\s\S]))`` alternate supplies that bound.
#:
#: Verified exactly equal to the native addon: 0 mismatches on 114558 corpus
#: inputs (19000 real source chunks, 2 mixed English/Chinese/Japanese/code
#: documents of 241 KB + 54 KB, 12000 emoji/ZWJ-heavy, 72591 per-codepoint fuzz,
#: 6824 random fuzz, 3064 targeted, 269 whitespace/digit, 808 adversarial), on
#: 341399 fresh fuzz inputs, and across every Unicode scalar in four contexts
#: (4448256 probes).
_CJK: Final = r"\u3040-\u30ff\u4e00-\u9fa5"
_PAT_DEEPSEEK: Final = "|".join(
    [
        r"[[\p{L}\p{M}]&&[" + _CJK + r"]]+",
        r"[[\p{P}\p{S}]&&[" + _CJK + r"]]+",
        r"[[^\r\n\p{L}\p{P}\p{S}]&&[" + _CJK + r"]][[\p{L}\p{M}]&&[" + _CJK + r"]]+",
        r"(?:[[^\p{L}\p{M}\p{N}\p{P}\p{S}\s]&&[" + _CJK + r"]](?![[\p{L}\p{M}]&&[" + _CJK + r"]]))+",
        r"\p{N}{1,3}",
        r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~][A-Za-z]+",
        r"[[^\r\n\p{L}\p{P}\p{S}]&&[^" + _CJK + r"]]?[[\p{L}\p{M}]&&[^" + _CJK + r"]]+",
        r" ?[[\p{P}\p{S}]&&[^" + _CJK + r"]]+[\r\n]*",
        r"\s*[\r\n]+",
        # A run reaching a stage-1/2 piece boundary keeps its whole length.
        r"\s+(?=[\p{N}" + _CJK + r"]|(?![\s\S]))",
        r"\s+(?!\S)",
        r"\s",
        r"(?:[[^\p{L}\p{M}\p{N}\p{P}\p{S}\s]&&[^" + _CJK + r"]](?![[\p{L}\p{M}]&&[^" + _CJK + r"]]))+",
    ]
)

#: Canonical encoding name -> (vocabulary file, splitter, unicode normalization).
#: Canonical names are omp's ``Encoding`` enum members. ``None`` as the
#: vocabulary file means the encoding is served by the Claude engine rather than
#: a single-vocabulary tiktoken build; its splitter and normalization are
#: ``None`` because that engine owns the whole pipeline.
_NormalizationForm = Literal["NFC", "NFD", "NFKC", "NFKD"]
_ENCODINGS: Final[dict[str, tuple[str | None, str | None, _NormalizationForm | None]]] = {
    "O200kBase": ("O200kBase.utok1.bz2", _PAT_O200K, None),
    "Cl100kBase": ("Cl100kBase.utok1.bz2", _PAT_CL100K, None),
    "Glm5": ("Glm5.utok1.bz2", _PAT_CL100K, None),
    "Qwen3": ("Qwen3.utok1.bz2", _PAT_QWEN3, "NFC"),
    "KimiK2": ("KimiK2.utok1.bz2", _PAT_O200K_NO_SLASH, None),
    "DeepSeekV3": ("DeepSeekV3.utok1.bz2", _PAT_DEEPSEEK, None),
    # The Claude encodings are not a pretokenizer over one vocabulary: they run
    # ctok's normalize -> mark -> min-cost-tile pipeline over a CTOK container.
    "ClaudeV3": (None, None, None),
    "ClaudeV47": (None, None, None),
    "ClaudeV5": (None, None, None),
    "ClaudeV5Sonnet": (None, None, None),
}

#: Fold a catalog tokenizer value to the canonical key above. omp's per-model
#: routing table spells the same encoding in kebab-case (``deepseek-v3``),
#: while its ``Encoding`` enum spells it in CamelCase (``DeepSeekV3``); both
#: name the same vocabulary.
_ALIASES: Final[dict[str, str]] = {name.lower().replace("-", ""): name for name in _ENCODINGS}


def _canonical(tokenizer: str) -> str | None:
    return _ALIASES.get(tokenizer.strip().lower().replace("-", ""))


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7


def _load_utok1(blob: bytes) -> list[bytes]:
    """Decode a UTOK1 container into its rank-ordered token list."""
    if blob[:6] != b"UTOK1\n":
        raise ValueError("tokenizer vocabulary is not a UTOK1 container")
    index = 6
    count = int.from_bytes(blob[index : index + 4], "little")
    index += 4
    tokens: list[bytes] = []
    for _ in range(count):
        length, index = _read_varint(blob, index)
        tokens.append(blob[index : index + length])
        index += length
    if index != len(blob):
        raise ValueError("tokenizer vocabulary container has trailing bytes")
    return tokens


@cache
def _encoding_for(name: str) -> tiktoken.Encoding:
    """Build (and cache) one encoding from its shipped vocabulary.

    Constructing ``tiktoken.Encoding`` directly from our own ranks keeps this
    entirely offline; ``tiktoken.get_encoding`` would instead fetch vocabulary
    data over HTTP on a cold cache.
    """
    filename, pattern, _ = _ENCODINGS[name]
    # Claude encodings carry no tiktoken vocabulary; ``count_tokens`` routes
    # them to the Claude engine before reaching here.
    assert filename is not None and pattern is not None, f"{name} has no tiktoken vocabulary"
    tokens = _load_utok1(bz2.decompress(files(_DATA_PACKAGE).joinpath(filename).read_bytes()))
    return tiktoken.Encoding(
        name=name,
        pat_str=pattern,
        mergeable_ranks={token: rank for rank, token in enumerate(tokens)},
        special_tokens={},
    )


def count_tokens(text: str, tokenizer: str | None = None) -> int:
    """Tokens ``text`` costs under ``tokenizer``, or omp's byte guess without one.

    ``tokenizer`` is the catalog value: an omp encoding name, in either the enum
    spelling (``DeepSeekV3``, ``ClaudeV47``) or the routing-table spelling
    (``deepseek-v3``, ``claude-v47``). An unknown or absent name falls back to
    ``(utf8_bytes + 3) >> 2``, the same number omp uses for a model with no
    tokenizer.
    """
    if not text:
        return 0
    name = _canonical(tokenizer) if tokenizer else None
    if name is None:
        return (len(text.encode("utf-8")) + 3) >> 2
    if name in _CLAUDE:
        return _claude_count_tokens(text, name)
    filename, _, normalization = _ENCODINGS[name]
    if normalization is not None:
        text = unicodedata.normalize(normalization, text)
    return len(_encoding_for(name).encode_ordinary(text))


def known_tokenizers() -> tuple[str, ...]:
    """Encoding names this build can count exactly (everything else falls back)."""
    return tuple(sorted(_ENCODINGS))
