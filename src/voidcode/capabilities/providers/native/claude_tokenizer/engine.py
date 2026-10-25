"""The min-cost tiling, with the marked-stream vocabulary indexed by a reverse trie.

    min_tile        generic segmentation DP for tiny byte strings
    min_vocab_tile  the same recurrence, visiting only vocabulary edges through the trie
    ByteFloor       byte tiling for characters no piece covers
    tile            marked-stream tiling for the count

Marker atoms are in the vocabulary as cost-1 tokens, so a marker no piece absorbs tiles as itself.
The count is the number of tiles.
"""

from __future__ import annotations

from .constants import BOW_G, CAPS_G, EOW_G, ESCAPED_MARKER_LITERALS, MARKER_GLYPHS, SHIFT_G
from .normalize import nfc, raw_head_space, stream_norm


def min_tile(n: int, cost_fn, max_len: int) -> int:
    """Minimum cost over byte segments, with a guaranteed single-byte floor."""
    best = [0] + [n + 1] * n
    for end in range(1, n + 1):
        for start in range(max(0, end - max_len), end):
            cost = cost_fn(start, end)
            if cost is not None:
                best[end] = min(best[end], best[start] + cost)
    return best[n]


class ReverseTrie:
    """Vocabulary index for the stream DP: pieces stored backwards, since the DP ends a tile at
    each position and scans candidate starts right to left."""

    _END = None

    def __init__(self, pieces) -> None:
        self.root = {}
        for piece in pieces:
            node = self.root
            for ch in reversed(piece):
                node = node.setdefault(ch, {})
            node[self._END] = None


def min_vocab_tile(text: str, trie: ReverseTrie, unit_cost) -> int:
    """Min-cost tiling over a cost-1 vocabulary plus a guaranteed one-character floor.

    The specialized form of :func:`min_tile`: the trie visits only prefixes that can still become
    a piece, instead of probing every substring up to the longest piece (128 for v3).
    """
    best = [0] * (len(text) + 1)
    root = trie.root
    terminal = trie._END
    for end in range(1, len(text) + 1):
        # The guaranteed one-character edge: the vocabulary spelling if it is a piece, else the
        # byte floor.
        start = end - 1
        node = root.get(text[start])
        best[end] = best[start] + (1 if node is not None and terminal in node else unit_cost(start))

        if node is None:
            continue
        for start in range(end - 2, -1, -1):
            node = node.get(text[start])
            if node is None:
                break
            if terminal in node:
                candidate = best[start] + 1
                if candidate <= best[end]:
                    best[end] = candidate

    return best[-1]


class ByteFloor:
    """What a codepoint costs when no piece covers it: a min-cost tiling of its UTF-8 bytes over
    the partial byte-prefix tokens, every single byte costing 1."""

    def __init__(self, byte_tokens, unit_chars=()) -> None:
        # Membership is all that is needed, since every token costs 1. ``unit_chars`` are the
        # cost-1 whole codepoints from the piece vocabulary.
        self.tokens = set(byte_tokens) | {c.encode().hex() for c in unit_chars}
        self.max_len = max((len(k) // 2 for k in self.tokens), default=1)
        self._costs: dict[bytes, int] = {}

    def cost_bytes(self, bs: bytes) -> int:
        """Minimum byte-token count, cached per byte string."""
        hit = self._costs.get(bs)
        if hit is None:

            def cost_fn(start: int, end: int) -> int | None:
                return 1 if end - start == 1 or bs[start:end].hex() in self.tokens else None

            hit = self._costs[bs] = min_tile(len(bs), cost_fn, self.max_len)
        return hit

    def cost_char(self, c: str) -> int:
        """Isolated codepoint cost via the byte floor."""
        return self.cost_bytes(c.encode()) if self.tokens else len(c.encode())


# ---- tiling the marked stream ---------------------------------------------------------------


def char_cost(model, ch: str) -> int:
    """One codepoint standing alone: 1 if it is itself a token, else its byte-floor tiling.
    The cache avoids repricing repeated characters in long runs."""
    ch = ESCAPED_MARKER_LITERALS.get(ch, ch)
    cache = model._char_cost_cache
    hit = cache.get(ch)
    if hit is None:
        hit = cache[ch] = 1 if ch in model.unit_pieces else model.bytes.cost_char(ch)
    return hit


def frame_tail(n: int, model) -> int:
    """What a content-final run of ``n`` frame-absorbed characters costs beyond the frame's own
    trailing token. Ladder families only; on v5 trailing whitespace is simply free.

    The frame appends ⏎⏎ after the content and one token can span into it, so the run the
    tokenizer sees is ``n + 2`` newlines, of which the frame already pays for one token. The cost
    is therefore not monotonic in ``n`` (28 trailing newlines are free, 29 cost one). Exact on all
    40 recorded rows; beyond 40 it rests on the vocabulary's newline ladder being complete.
    """
    if n == 0 or model.frame_tail != "ladder":
        return 0
    # The frame appends ⏎⏎ (``appended_newlines``), so one token can span into it; the
    # sonnet/fable-5 frame appends nothing.
    run = "\n" * (n + model.appended_newlines)

    return min_vocab_tile(run, model.trie, lambda _at: 1) - 1  # the last token belongs to the frame


def tile(text: str, model) -> int:
    """Count the minimum-cost tokens in the marked stream and frame tail."""
    if model.frame_tail == "ladder":
        norm = nfc(text, fold_quotes=model.fold_quotes)
        n_tail = len(norm) - len(norm.rstrip(model.frame_strip))
    else:
        # The frame absorbs raw ASCII whitespace, so strip before NFC: `nfc` folds NBSP etc. to
        # U+0020, and those are not free at the end (`'a\xa0'` costs one more than `'a'`).
        norm = nfc(text.rstrip(model.frame_strip), fold_quotes=model.fold_quotes)
        n_tail = 0
    s = stream_norm(norm, model, raw_head_space=raw_head_space(text))
    tail = frame_tail(n_tail, model)

    def unit_floor(j: int) -> int:
        """One character where no piece covers it: a marker is a token, anything else falls to the
        byte floor."""
        ch = s[j]
        if ch in MARKER_GLYPHS:
            return 1
        return char_cost(model, ch)

    return min_vocab_tile(s, model.trie, unit_floor) + tail


# ---- the shipped vocabulary container ------------------------------------------------------------
#
# ``CTOK\x02`` layout, verified to consume the blob exactly:
#
#   0x00  magic "CTOK\x02" (5 bytes)
#   0x05  u8        0x01 (v3) / 0x00 (v4.7)
#   0x06  u8 x2     version scalars
#   0x08  u16 LE    byte-prefix table entry count (467)
#   0x0a  u32 LE    vocabulary entry count
#   0x0e  table     467 x [u8 n][n bytes], bytewise sorted UTF-8 prefixes
#   0x58b vocabulary N x [u8 shared][u8 suffix_len][suffix], front-coded against
#                   the previous entry, bytewise sorted
#
# The table is the byte floor: the longest prefix that matches the head of an
# uncovered character's UTF-8, else one byte. The vocabulary is the tiling
# vocabulary in the byte marker spelling.

_MAGIC = b"CTOK\x02"
_TABLE_END = 0x58B


class VocabCore:
    """One loaded vocabulary: pieces, the byte floor, and frame parameters."""

    __slots__ = (
        "vocab",
        "trie",
        "unit_pieces",
        "bytes",
        "fold_quotes",
        "allcaps_min",
        "frame_bow",
        "frame_tail",
        "frame_strip",
        "appended_newlines",
        "_char_cost_cache",
    )

    def __init__(
        self,
        vocab: set[str],
        byte_tokens: set[bytes],
        *,
        fold_quotes: bool,
        allcaps_min: int | None,
        frame_bow: bool,
        frame_tail: str,
        appended_newlines: int,
    ) -> None:
        self.vocab = frozenset(vocab)
        self.trie = ReverseTrie(self.vocab)
        self.unit_pieces = {p for p in vocab if len(p) == 1 and p not in MARKER_GLYPHS}
        # ByteFloor keys on hex strings, the container's own spelling.
        self.bytes = ByteFloor({t.hex() for t in byte_tokens}, self.unit_pieces)
        self.fold_quotes = fold_quotes
        self.allcaps_min = allcaps_min
        self.frame_bow = frame_bow
        self.frame_tail = frame_tail
        self.frame_strip = "\n" if frame_tail == "ladder" else " \t\n\r\f\v"
        self.appended_newlines = appended_newlines
        self._char_cost_cache: dict[str, int] = {}

    @classmethod
    def parse(
        cls,
        blob: bytes,
        *,
        fold_quotes: bool,
        allcaps_min: int | None,
        frame_bow: bool,
        frame_tail: str,
        appended_newlines: int,
    ) -> VocabCore:
        if blob[:5] != _MAGIC:
            raise ValueError("tokenizer vocabulary is not a CTOK container")
        index = 0x0E
        count = int.from_bytes(blob[0x0A:0x0E], "little")
        byte_tokens: set[bytes] = set()
        while index < _TABLE_END:
            size = blob[index]
            index += 1
            byte_tokens.add(blob[index : index + size])
            index += size
        if index != _TABLE_END:
            raise ValueError("CTOK byte-prefix table does not fit its section")
        # Markers are written as C0 bytes in the container and as ctok's
        # noncharacter glyphs in the encoder; map them back so the two agree.
        # The shared-prefix length counts bytes, so a suffix may begin
        # mid-character: accumulate bytes and decode whole tokens.
        vocab: set[str] = set()
        prefix = b""
        count_seen = 0
        while index < len(blob):
            shared = blob[index]
            index += 1
            length = blob[index]
            index += 1
            token = prefix[:shared] + blob[index : index + length]
            index += length
            prefix = token
            count_seen += 1
            vocab.add("".join(_MARKER_BYTES.get(c, c) for c in token.decode("utf-8")))
        if count_seen != count:
            raise ValueError("CTOK vocabulary count does not match its header")
        return cls(
            vocab,
            byte_tokens,
            fold_quotes=fold_quotes,
            allcaps_min=allcaps_min,
            frame_bow=frame_bow,
            frame_tail=frame_tail,
            appended_newlines=appended_newlines,
        )


#: Container marker byte -> the glyph the encoder writes. ``0x03`` is ⟨pad⟩,
#: which never appears in the stream and so has no glyph here.
_MARKER_BYTES = {"\x01": BOW_G, "\x02": EOW_G, "\x04": SHIFT_G, "\x05": CAPS_G}
