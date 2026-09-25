"""Pure, stateful byte -> key decoder for terminal input.

Mirrors omp's canonical key normalisation (``crates/pi-natives/src/keys.rs``
and ``packages/tui/src/keys.ts``) as captured in
``.omo/plans/tui-input-spec.md``. No terminal I/O, no ``rich``, no imports
from other ``voidcode.tui`` modules -- this module stays dependency-free so it
can be exercised headlessly.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Key", "KeyDecoder"]


# --- modifier bitmask (spec 1.2) -------------------------------------------
_MOD_SHIFT = 1
_MOD_ALT = 2
_MOD_CTRL = 4
_MOD_SUPER = 8
_MOD_NUM_LOCK = 128
_LOCK_MASK = 64 + _MOD_NUM_LOCK
_SUPPORTED_MODS = _MOD_SHIFT | _MOD_ALT | _MOD_CTRL | _MOD_SUPER

# --- bracketed paste / protocol bytes (spec 2.1, 9.2) ----------------------
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_PASTE_MODE_SEQUENCES = (b"\x1b[?2004h", b"\x1b[?2004l")
_MAX_CSI_BYTES = 4096
_PASTE_MAX_BYTES = 64 * 1024 * 1024

# --- legacy escape sequences (spec 1.4, verbatim from keys.rs:175-207) -----
_LEGACY: dict[bytes, str] = {
    b"\x1bOA": "up",
    b"\x1bOB": "down",
    b"\x1bOC": "right",
    b"\x1bOD": "left",
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1bOH": "home",
    b"\x1bOF": "end",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1b[1~": "home",
    b"\x1b[7~": "home",
    b"\x1b[4~": "end",
    b"\x1b[8~": "end",
    b"\x1b[E": "clear",
    b"\x1bOE": "clear",
    b"\x1bOe": "ctrl+clear",
    b"\x1b[e": "shift+clear",
    b"\x1b[2~": "insert",
    b"\x1b[2$": "shift+insert",
    b"\x1b[2^": "ctrl+insert",
    b"\x1b[3~": "delete",
    b"\x1b[3$": "shift+delete",
    b"\x1b[3^": "ctrl+delete",
    b"\x1b[5~": "pageUp",
    b"\x1b[6~": "pageDown",
    b"\x1b[[5~": "pageUp",
    b"\x1b[[6~": "pageDown",
    b"\x1b[a": "shift+up",
    b"\x1b[b": "shift+down",
    b"\x1b[c": "shift+right",
    b"\x1b[d": "shift+left",
    b"\x1bOa": "ctrl+up",
    b"\x1bOb": "ctrl+down",
    b"\x1bOc": "ctrl+right",
    b"\x1bOd": "ctrl+left",
    b"\x1b[5$": "shift+pageUp",
    b"\x1b[6$": "shift+pageDown",
    b"\x1b[7$": "shift+home",
    b"\x1b[8$": "shift+end",
    b"\x1b[5^": "ctrl+pageUp",
    b"\x1b[6^": "ctrl+pageDown",
    b"\x1b[7^": "ctrl+home",
    b"\x1b[8^": "ctrl+end",
    b"\x1bOP": "f1",
    b"\x1bOQ": "f2",
    b"\x1bOR": "f3",
    b"\x1bOS": "f4",
    b"\x1b[11~": "f1",
    b"\x1b[12~": "f2",
    b"\x1b[13~": "f3",
    b"\x1b[14~": "f4",
    b"\x1b[[A": "f1",
    b"\x1b[[B": "f2",
    b"\x1b[[C": "f3",
    b"\x1b[[D": "f4",
    b"\x1b[[E": "f5",
    b"\x1b[15~": "f5",
    b"\x1b[17~": "f6",
    b"\x1b[18~": "f7",
    b"\x1b[19~": "f8",
    b"\x1b[20~": "f9",
    b"\x1b[21~": "f10",
    b"\x1b[23~": "f11",
    b"\x1b[24~": "f12",
}

_LEGACY_KEYS = tuple(sorted(_LEGACY, key=len, reverse=True))

# --- functional-key table (spec 1.11, keys.rs:1680-1720) -------------------
_FUNCTIONAL: dict[int, str] = {
    1: "home",
    2: "insert",
    3: "delete",
    4: "end",
    5: "pageUp",
    6: "pageDown",
    7: "home",
    8: "end",
    11: "f1",
    12: "f2",
    13: "f3",
    14: "f4",
    15: "f5",
    17: "f6",
    18: "f7",
    19: "f8",
    20: "f9",
    21: "f10",
    23: "f11",
    24: "f12",
}

# --- CSI 1;mod <letter> table (spec 1.10) ----------------------------------
_CSI_1_LETTER: dict[int, str] = {
    0x41: "up",
    0x42: "down",
    0x43: "right",
    0x44: "left",
    0x48: "home",
    0x46: "end",
    0x45: "clear",
    0x50: "f1",
    0x51: "f2",
    0x52: "f3",
    0x53: "f4",
}

# --- keypad codepoints (spec 1.13) -----------------------------------------
_KEYPAD_NAV: dict[int, str] = {
    57399: "insert",
    57400: "end",
    57401: "down",
    57402: "pageDown",
    57403: "left",
    57404: "clear",
    57405: "right",
    57406: "home",
    57407: "up",
    57408: "pageUp",
    57409: "delete",
    57414: "enter",
}
_KEYPAD_TEXT: dict[int, str] = {
    57399: "0",
    57400: "1",
    57401: "2",
    57402: "3",
    57403: "4",
    57404: "5",
    57405: "6",
    57406: "7",
    57407: "8",
    57408: "9",
    57409: ".",
}
_KEYPAD_OP_TEXT: dict[int, str] = {57410: "/", 57411: "*", 57412: "-", 57413: "+", 57415: "="}

# --- named codepoints (spec 1.3, keys.rs:44-66 + format_key_name) ----------
_NAMED_CODEPOINTS: dict[int, str] = {
    27: "escape",
    9: "tab",
    13: "enter",
    32: "space",
    127: "backspace",
    **_KEYPAD_NAV,
}

_SYMBOLS = frozenset(ord(c) for c in "`\"-=[]\\;',./!@#$%^&*()_+|~{}:<>?")
_CTRL_SYMBOLS = {0x1C: "\\", 0x1D: "]", 0x1E: "^", 0x1F: "_"}


class _Marker:
    """Internal marker returned by parsers that cannot yield a token yet."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


_INVALID_UTF8 = _Marker("invalid-utf8")
_INCOMPLETE = _Marker("incomplete")
_CSI_INVALID = _Marker("csi-invalid")


@dataclass(frozen=True, slots=True)
class Key:
    """A decoded terminal key.

    ``name`` is the canonical key name (``"a"``, ``"enter"``, ``"ctrl+c"``,
    ``"shift+enter"``, ``"paste"``, ``"unknown"``, ...). ``text`` carries the
    printable text the key inserts, and is ``""`` for everything that is not
    plain text (control bytes, named keys, modifier chords, ``unknown``
    payloads aside).

    Non-ASCII printable characters are named by themselves: ``feed`` of the
    UTF-8 bytes for ``"你"`` yields ``Key("你", "你")``.
    """

    name: str
    text: str = ""


def _unknown(raw: bytes) -> Key:
    """Unrecognised input. ``text`` carries the raw bytes (lossy UTF-8)."""
    return Key("unknown", raw.decode("utf-8", "replace"))


def _is_ascii_letter(cp: int) -> bool:
    return 0x41 <= cp <= 0x5A or 0x61 <= cp <= 0x7A


def _is_symbol_key(cp: int) -> bool:
    return cp in _SYMBOLS


def _format_key_name(cp: int) -> str | None:
    name = _NAMED_CODEPOINTS.get(cp)
    if name is not None:
        return name
    if 33 <= cp <= 126:
        return chr(cp)
    return None


def _format_with_mods(mods: int, key_name: str) -> str:
    parts = []
    if mods & _MOD_SHIFT:
        parts.append("shift")
    if mods & _MOD_CTRL:
        parts.append("ctrl")
    if mods & _MOD_ALT:
        parts.append("alt")
    if mods & _MOD_SUPER:
        parts.append("super")
    parts.append(key_name)
    return "+".join(parts)


def _esc_pair(code: int, kitty_active: bool) -> str | None:
    """spec 1.7 -- ``ESC`` + one byte (``parse_esc_pair``)."""
    if code in (0x7F, 0x08):
        return "alt+backspace"
    if code in (0x0D, 0x0A):
        return "alt+enter"
    if code == 0x09:
        return "alt+tab"
    if not kitty_active:
        if code == 0x20:
            return "alt+space"
        if code == 0x42:  # "B"
            return "alt+left"
        if code == 0x46:  # "F"
            return "alt+right"
    if 0x01 <= code <= 0x1A:
        return f"ctrl+alt+{chr(ord('a') + code - 1)}"
    if 0x61 <= code <= 0x7A:
        return f"alt+{chr(code)}"
    if 0x41 <= code <= 0x5A:
        return f"alt+shift+{chr(code + 32)}"
    return None


def _single_byte(code: int) -> Key:
    """spec 1.5 -- single byte parse (named keys win over Ctrl collisions)."""
    if code == 0x1B:
        return Key("escape")
    if code == 0x09:
        return Key("tab")
    if code in (0x0A, 0x0D):
        return Key("enter")
    if code in (0x08, 0x7F):
        return Key("backspace")
    if code == 0x00:
        return Key("ctrl+space")
    if code == 0x20:
        return Key("space", " ")
    if 0x01 <= code <= 0x1A:
        return Key(f"ctrl+{chr(ord('a') + code - 1)}")
    if code in _CTRL_SYMBOLS:
        return Key(f"ctrl+{_CTRL_SYMBOLS[code]}")
    if 0x21 <= code <= 0x7E:
        return Key(chr(code), chr(code))
    return _unknown(bytes([code]))


def _digits(buf: bytes, idx: int, end: int) -> tuple[int | None, int]:
    start = idx
    while idx < end and 0x30 <= buf[idx] <= 0x39:
        idx += 1
    if idx == start:
        return None, start
    return int(buf[start:idx]), idx


def _csi_extent(buf: bytes) -> bytes | _Marker:
    """Return the complete CSI/SS3 token at the head of ``buf``.

    ``_INCOMPLETE`` when more bytes may extend it, ``_CSI_INVALID`` when the
    byte stream cannot be a CSI/SS3 sequence.
    """
    if buf.startswith(b"\x1b["):
        i = 2
        while i < len(buf):
            byte = buf[i]
            if 0x20 <= byte <= 0x3F:  # parameter / intermediate bytes
                i += 1
                continue
            if 0x40 <= byte <= 0x7E:  # final byte
                return buf[: i + 1]
            return _CSI_INVALID
        return _INCOMPLETE
    if buf.startswith(b"\x1bO"):
        return buf[:3] if len(buf) >= 3 else _INCOMPLETE
    return _CSI_INVALID


@dataclass(frozen=True, slots=True)
class _Kitty:
    codepoint: int
    shifted: int | None
    base: int | None
    text_field: str
    modifier: int
    event_type: int | None


def _parse_csi_u(seq: bytes) -> _Kitty | None:
    end = len(seq) - 1  # index of the "u" terminator
    idx = 2
    value, idx = _digits(seq, idx, end)
    if value is None:
        return None
    codepoint = value
    shifted: int | None = None
    base: int | None = None
    if idx < end and seq[idx] == 0x3A:  # ':'
        idx += 1
        shifted, idx = _digits(seq, idx, end)
        if idx < end and seq[idx] == 0x3A:
            idx += 1
            value, idx = _digits(seq, idx, end)
            if value is None:
                return None
            base = value

    mod_value = 1
    event_type: int | None = None
    if idx < end and seq[idx] == 0x3B:  # ';'
        idx += 1
        if idx < end and 0x30 <= seq[idx] <= 0x39:
            value, idx = _digits(seq, idx, end)
            mod_value = value if value is not None else 1
        else:
            mod_value = 1
        if idx < end and seq[idx] == 0x3A:
            idx += 1
            event_type, idx = _digits(seq, idx, end)
            if event_type is None:
                return None

    text_field = ""
    if idx < end and seq[idx] == 0x3B:
        idx += 1
        start = idx
        while idx < end:
            if seq[idx] == 0x3A:
                idx += 1
                continue
            value, idx = _digits(seq, idx, end)
            if value is None:
                return None
        text_field = seq[start:end].decode("ascii")

    if idx != end or mod_value == 0:
        return None
    return _Kitty(codepoint, shifted, base, text_field, mod_value - 1, event_type)


def _parse_functional(seq: bytes) -> tuple[str, int, int | None] | None:
    end = len(seq) - 1  # index of '~'
    idx = 2
    key_num, idx = _digits(seq, idx, end)
    if key_num is None:
        return None
    mod_value = 1
    if idx < end and seq[idx] == 0x3B:
        idx += 1
        value, idx = _digits(seq, idx, end)
        if value is None:
            return None
        mod_value = value
    event_type: int | None = None
    if idx < end and seq[idx] == 0x3A:
        idx += 1
        event_type, idx = _digits(seq, idx, end)
        if event_type is None:
            return None
    if idx != end or mod_value == 0:
        return None
    name = _FUNCTIONAL.get(key_num)
    if name is None:
        return None
    return name, mod_value - 1, event_type


def _parse_csi_1_letter(seq: bytes) -> tuple[str, int, int | None] | None:
    if not seq.startswith(b"\x1b[1;"):
        return None
    end = len(seq)
    idx = 4
    mod_value, idx = _digits(seq, idx, end)
    if mod_value is None:
        return None
    event_type: int | None = None
    if idx < end and seq[idx] == 0x3A:
        idx += 1
        event_type, idx = _digits(seq, idx, end)
        if event_type is None:
            return None
    if idx + 1 != end or mod_value == 0:
        return None
    name = _CSI_1_LETTER.get(seq[idx])
    if name is None:
        return None
    return name, mod_value - 1, event_type


def _parse_modify_other_keys(seq: bytes) -> tuple[int, int] | None:
    """spec 1.12 -- ``CSI 27 ; modifiers ; keycode [~]``."""
    if len(seq) < 7 or not seq.startswith(b"\x1b[27;"):
        return None
    end = len(seq) - 1 if seq[-1] == 0x7E else len(seq)
    if end <= 5:
        return None
    idx = 5
    mod_value, idx = _digits(seq, idx, end)
    if mod_value is None or idx >= end or seq[idx] != 0x3B:
        return None
    idx += 1
    keycode, idx = _digits(seq, idx, end)
    if keycode is None or idx != end or mod_value == 0:
        return None
    return mod_value - 1, keycode


def _text_field_codepoints(field: str) -> list[int]:
    out = []
    for part in field.split(":"):
        if part:
            out.append(int(part))
    return out


def _first_text_codepoint(field: str) -> int | None:
    codepoints = _text_field_codepoints(field)
    if len(codepoints) != 1:
        return None
    cp = codepoints[0]
    if cp < 32 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
        return None
    return cp


def _chr_or_empty(cp: int) -> str:
    try:
        return chr(cp)
    except ValueError:
        return ""


def _kitty_text(parsed: _Kitty) -> str:
    """Printable text for a CSI-u sequence (mirrors ``decodeKittyPrintable``)."""
    effective = parsed.modifier & ~_LOCK_MASK
    if effective & ~_SUPPORTED_MODS:
        return ""
    if effective & (_MOD_ALT | _MOD_CTRL | _MOD_SUPER):
        return ""
    codepoints = [cp for cp in _text_field_codepoints(parsed.text_field) if cp >= 32 and cp != 127]
    if codepoints:
        return "".join(_chr_or_empty(cp) for cp in codepoints)
    operator = _KEYPAD_OP_TEXT.get(parsed.codepoint)
    if operator is not None:
        return operator
    if effective == 0:
        numpad = _KEYPAD_TEXT.get(parsed.codepoint)
        if numpad is not None:
            return numpad
    cp = parsed.codepoint
    if effective & _MOD_SHIFT and parsed.shifted is not None:
        cp = parsed.shifted
    if 0xE000 <= cp <= 0xF8FF:
        return ""
    if cp < 32 or cp == 127:
        return ""
    return _chr_or_empty(cp)


def _format_kitty(parsed: _Kitty) -> Key | None:
    """Mirror ``format_kitty_key`` (keys.rs:1504-1560)."""
    effective = parsed.modifier & ~_LOCK_MASK
    if effective & ~_SUPPORTED_MODS:
        return None
    operator = _KEYPAD_OP_TEXT.get(parsed.codepoint)
    if operator is not None:
        effective_codepoint = ord(operator)
    else:
        cp = parsed.codepoint
        if effective == 0 or _is_ascii_letter(cp) or _is_symbol_key(cp):
            effective_codepoint = cp
        else:
            effective_codepoint = parsed.base if parsed.base is not None else cp

    text = _kitty_text(parsed)
    if effective == 0:
        text_cp = _first_text_codepoint(parsed.text_field)
        if text_cp is not None:
            name = _format_key_name(text_cp)
            if name is not None:
                return Key(name, text)
        numpad = _KEYPAD_TEXT.get(parsed.codepoint)
        if numpad is not None:
            return Key(numpad, text)
        name = _format_key_name(effective_codepoint)
        if name is None:
            # Non-ASCII printable codepoints have no canonical name; name them
            # by the character they produce (same rule as raw UTF-8 input).
            return Key(text, text) if text else None
        return Key(name, text)

    name = _format_key_name(effective_codepoint)
    if name is None:
        return None
    return Key(_format_with_mods(effective, name), text)


def _kitty_token(seq: bytes) -> Key | None:
    """Decode a complete kitty/enhanced sequence, or ``None`` if unknown."""
    if not seq:
        return None
    terminator = seq[-1]
    if terminator == 0x75:  # 'u'
        parsed = _parse_csi_u(seq)
    elif terminator == 0x7E:  # '~'
        functional = _parse_functional(seq)
        return _functional_key(*functional) if functional is not None else None
    elif terminator in _CSI_1_LETTER:
        letter = _parse_csi_1_letter(seq)
        return _functional_key(*letter) if letter is not None else None
    else:
        return None
    if parsed is None:
        return None
    if parsed.event_type == 3:
        return None  # key release -- no user-visible key
    return _format_kitty(parsed)


def _functional_key(name: str, mod_value: int, event_type: int | None) -> Key | None:
    if event_type == 3:
        return None
    effective = mod_value & ~_LOCK_MASK
    if effective & ~_SUPPORTED_MODS:
        return None
    if effective == 0:
        return Key(name)
    return Key(_format_with_mods(effective, name))


class KeyDecoder:
    """Incremental byte -> :class:`Key` decoder.

    A partial escape sequence or a half-received UTF-8 character fed alone is
    buffered, never guessed. Instances hold no global state; two decoders do
    not interfere. ``flush()`` resolves whatever is still buffered.
    """

    def __init__(self, *, kitty_active: bool = False) -> None:
        self._buffer = bytearray()
        self._paste = False
        self._kitty_active = kitty_active

    def feed(self, data: bytes) -> list[Key]:
        """Decode as much of ``data`` as possible, buffering partial input."""
        self._buffer.extend(data)
        out: list[Key] = []
        while self._buffer:
            raw = bytes(self._buffer)
            if self._paste:
                end = self._buffer.find(_PASTE_END)
                if end < 0:
                    if len(self._buffer) > _PASTE_MAX_BYTES:
                        out.append(self._finish_paste())
                    break
                out.append(self._finish_paste(end))
                continue
            if len(raw) < len(_PASTE_START) and _is_paste_marker_prefix(raw):
                break
            if raw.startswith(_PASTE_START):
                del self._buffer[: len(_PASTE_START)]
                self._paste = True
                continue
            result = self._decode_one(raw)
            if result is None:
                if len(self._buffer) > _MAX_CSI_BYTES:
                    out.append(_unknown(raw))
                    self._buffer.clear()
                break
            key, consumed = result
            out.append(key)
            del self._buffer[:consumed]
        return out

    def flush(self) -> list[Key]:
        """Decode whatever is pending (EOF/abort); never discards bytes."""
        out: list[Key] = []
        while self._buffer:
            if self._paste:
                out.append(self._finish_paste())
                continue
            raw = bytes(self._buffer)
            result = self._decode_one(raw)
            if result is not None:
                key, consumed = result
                out.append(key)
                del self._buffer[:consumed]
                continue
            self._buffer.clear()
            if all(byte == 0x1B for byte in raw):
                out.extend(Key("escape") for _ in raw)
                continue
            modify_other = _parse_modify_other_keys(raw)
            if modify_other is not None:
                key = _modify_other_keys_key(*modify_other)
                out.append(key if key is not None else _unknown(raw))
                continue
            out.append(_unknown(raw))
        return out

    # -- internals ----------------------------------------------------------

    def _finish_paste(self, end: int | None = None) -> Key:
        if end is None:
            body = bytes(self._buffer)
            self._buffer.clear()
        else:
            body = bytes(self._buffer[:end])
            del self._buffer[: end + len(_PASTE_END)]
        self._paste = False
        return Key("paste", body.decode("utf-8", "replace"))

    def _decode_one(self, buf: bytes) -> tuple[Key, int] | None:
        byte = buf[0]
        if byte == 0x1B:
            return self._decode_escape(buf)
        if byte >= 0x80:
            decoded = _utf8_char(buf)
            if decoded is None:
                return None
            if isinstance(decoded, _Marker):
                return _unknown(buf[:1]), 1
            char, consumed = decoded
            return Key(char, char), consumed
        return _single_byte(byte), 1

    def _decode_escape(self, buf: bytes) -> tuple[Key, int] | None:
        if len(buf) < 2:
            return None
        if buf[1] == 0x1B:
            if len(buf) == 2:
                return None
            if buf[2] in (0x5B, 0x4F):  # '[' or 'O' -- meta-CSI / meta-SS3
                inner = self._decode_escape(buf[1:])
                if inner is None:
                    return None
                key, consumed = inner
                return Key(f"alt+{key.name}"), consumed + 1
            return Key("escape"), 1

        if buf[1] != 0x4F:  # 'O' starts SS3, never a two-byte Alt chord
            pair = _esc_pair(buf[1], self._kitty_active)
            if pair is not None:
                return Key(pair), 2

        for sequence in _LEGACY_KEYS:
            if buf.startswith(sequence):
                return Key(_LEGACY[sequence]), len(sequence)

        for fixed in (b"\x1b[Z", b"\x1bOM"):
            if buf.startswith(fixed):
                return (Key("shift+tab") if fixed == b"\x1b[Z" else Key("enter")), len(fixed)

        if any(sequence.startswith(buf) for sequence in _LEGACY_KEYS):
            return None

        token = _csi_extent(buf)
        if isinstance(token, _Marker):
            return None if token is _INCOMPLETE else (_unknown(buf[:2]), 2)

        if token in _PASTE_MODE_SEQUENCES:
            return _unknown(token), len(token)
        modify_other = _parse_modify_other_keys(token)
        if modify_other is not None:
            key = _modify_other_keys_key(*modify_other)
            return (key if key is not None else _unknown(token)), len(token)
        kitty = _kitty_token(token)
        if kitty is not None:
            return kitty, len(token)
        return _unknown(token), len(token)


def _modify_other_keys_key(modifier: int, keycode: int) -> Key | None:
    name = _format_key_name(keycode)
    if name is None:
        return None
    effective = modifier & ~_LOCK_MASK
    text = _chr_or_empty(keycode) if 32 <= keycode and keycode != 127 and (effective & ~_MOD_SHIFT) == 0 else ""
    if effective == 0:
        return Key(name, text)
    return Key(_format_with_mods(effective, name), text)


def _is_paste_marker_prefix(raw: bytes) -> bool:
    return _PASTE_START.startswith(raw) or _PASTE_END.startswith(raw)


def _utf8_char(buf: bytes) -> tuple[str, int] | _Marker | None:
    """Decode one UTF-8 character: ``(char, n)``, ``_INVALID_UTF8`` or ``None``."""
    lead = buf[0]
    if 0xC2 <= lead <= 0xDF:
        length = 2
    elif 0xE0 <= lead <= 0xEF:
        length = 3
    elif 0xF0 <= lead <= 0xF4:
        length = 4
    else:
        return _INVALID_UTF8
    if len(buf) < length:
        for byte in buf[1:]:
            if not 0x80 <= byte <= 0xBF:
                return _INVALID_UTF8
        return None
    try:
        return buf[:length].decode("utf-8"), length
    except UnicodeDecodeError:
        return _INVALID_UTF8
