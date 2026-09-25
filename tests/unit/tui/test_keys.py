"""Byte -> key decoder tests.

The corpus mirrors the key tables in ``.omo/plans/tui-input-spec.md`` and the
omp sources it cites (``crates/pi-natives/src/keys.rs``,
``packages/tui/src/keys.ts``). Expectations are ``Key`` values, so both the
canonical name and the inserted text are pinned.
"""

from __future__ import annotations

import pytest

from voidcode.tui.keys import Key, KeyDecoder


def _decode(data: bytes, *, kitty_active: bool = False) -> list[Key]:
    decoder = KeyDecoder(kitty_active=kitty_active)
    return decoder.feed(data) + decoder.flush()


# (id, bytes, expected keys)
CORPUS: list[tuple[str, bytes, list[Key]]] = [
    # --- CSI / SS3 navigation (spec 1.4) ---------------------------------
    ("csi-up", b"\x1b[A", [Key("up")]),
    ("csi-down", b"\x1b[B", [Key("down")]),
    ("csi-right", b"\x1b[C", [Key("right")]),
    ("csi-left", b"\x1b[D", [Key("left")]),
    ("ss3-up", b"\x1bOA", [Key("up")]),
    ("ss3-down", b"\x1bOB", [Key("down")]),
    ("ss3-right", b"\x1bOC", [Key("right")]),
    ("ss3-left", b"\x1bOD", [Key("left")]),
    ("csi-home", b"\x1b[H", [Key("home")]),
    ("csi-end", b"\x1b[F", [Key("end")]),
    ("ss3-home", b"\x1bOH", [Key("home")]),
    ("ss3-end", b"\x1bOF", [Key("end")]),
    ("home-tilde-1", b"\x1b[1~", [Key("home")]),
    ("home-tilde-7", b"\x1b[7~", [Key("home")]),
    ("end-tilde-4", b"\x1b[4~", [Key("end")]),
    ("end-tilde-8", b"\x1b[8~", [Key("end")]),
    ("clear-csi", b"\x1b[E", [Key("clear")]),
    ("clear-ss3", b"\x1bOE", [Key("clear")]),
    ("ctrl-clear", b"\x1bOe", [Key("ctrl+clear")]),
    ("shift-clear", b"\x1b[e", [Key("shift+clear")]),
    ("insert", b"\x1b[2~", [Key("insert")]),
    ("shift-insert", b"\x1b[2$", [Key("shift+insert")]),
    ("ctrl-insert", b"\x1b[2^", [Key("ctrl+insert")]),
    ("delete", b"\x1b[3~", [Key("delete")]),
    ("shift-delete", b"\x1b[3$", [Key("shift+delete")]),
    ("ctrl-delete", b"\x1b[3^", [Key("ctrl+delete")]),
    ("pageup", b"\x1b[5~", [Key("pageUp")]),
    ("pagedown", b"\x1b[6~", [Key("pageDown")]),
    ("pageup-linux", b"\x1b[[5~", [Key("pageUp")]),
    ("pagedown-linux", b"\x1b[[6~", [Key("pageDown")]),
    ("shift-up", b"\x1b[a", [Key("shift+up")]),
    ("shift-down", b"\x1b[b", [Key("shift+down")]),
    ("shift-right", b"\x1b[c", [Key("shift+right")]),
    ("shift-left", b"\x1b[d", [Key("shift+left")]),
    ("ctrl-up-ss3", b"\x1bOa", [Key("ctrl+up")]),
    ("ctrl-down-ss3", b"\x1bOb", [Key("ctrl+down")]),
    ("ctrl-right-ss3", b"\x1bOc", [Key("ctrl+right")]),
    ("ctrl-left-ss3", b"\x1bOd", [Key("ctrl+left")]),
    ("shift-pageup", b"\x1b[5$", [Key("shift+pageUp")]),
    ("shift-pagedown", b"\x1b[6$", [Key("shift+pageDown")]),
    ("shift-home", b"\x1b[7$", [Key("shift+home")]),
    ("shift-end", b"\x1b[8$", [Key("shift+end")]),
    ("ctrl-pageup", b"\x1b[5^", [Key("ctrl+pageUp")]),
    ("ctrl-pagedown", b"\x1b[6^", [Key("ctrl+pageDown")]),
    ("ctrl-home", b"\x1b[7^", [Key("ctrl+home")]),
    ("ctrl-end", b"\x1b[8^", [Key("ctrl+end")]),
    # --- function keys (spec 1.4) ----------------------------------------
    ("f1-ss3", b"\x1bOP", [Key("f1")]),
    ("f2-ss3", b"\x1bOQ", [Key("f2")]),
    ("f3-ss3", b"\x1bOR", [Key("f3")]),
    ("f4-ss3", b"\x1bOS", [Key("f4")]),
    ("f1-csi", b"\x1b[11~", [Key("f1")]),
    ("f2-csi", b"\x1b[12~", [Key("f2")]),
    ("f3-csi", b"\x1b[13~", [Key("f3")]),
    ("f4-csi", b"\x1b[14~", [Key("f4")]),
    ("f1-linux", b"\x1b[[A", [Key("f1")]),
    ("f2-linux", b"\x1b[[B", [Key("f2")]),
    ("f3-linux", b"\x1b[[C", [Key("f3")]),
    ("f4-linux", b"\x1b[[D", [Key("f4")]),
    ("f5-linux", b"\x1b[[E", [Key("f5")]),
    ("f5", b"\x1b[15~", [Key("f5")]),
    ("f6", b"\x1b[17~", [Key("f6")]),
    ("f7", b"\x1b[18~", [Key("f7")]),
    ("f8", b"\x1b[19~", [Key("f8")]),
    ("f9", b"\x1b[20~", [Key("f9")]),
    ("f10", b"\x1b[21~", [Key("f10")]),
    ("f11", b"\x1b[23~", [Key("f11")]),
    ("f12", b"\x1b[24~", [Key("f12")]),
    # --- single bytes (spec 1.5) -----------------------------------------
    ("escape", b"\x1b", [Key("escape")]),
    ("tab", b"\t", [Key("tab")]),
    ("enter-cr", b"\r", [Key("enter")]),
    ("enter-lf", b"\n", [Key("enter")]),
    ("backspace-del", b"\x7f", [Key("backspace")]),
    ("backspace-bs", b"\x08", [Key("backspace")]),
    ("ctrl-space", b"\x00", [Key("ctrl+space")]),
    ("ctrl-a", b"\x01", [Key("ctrl+a")]),
    ("ctrl-c", b"\x03", [Key("ctrl+c")]),
    ("ctrl-d", b"\x04", [Key("ctrl+d")]),
    ("ctrl-z", b"\x1a", [Key("ctrl+z")]),
    ("ctrl-backslash", b"\x1c", [Key("ctrl+\\")]),
    ("ctrl-bracket", b"\x1d", [Key("ctrl+]")]),
    ("ctrl-caret", b"\x1e", [Key("ctrl+^")]),
    ("ctrl-underscore", b"\x1f", [Key("ctrl+_")]),
    ("space", b" ", [Key("space", " ")]),
    ("letter-a", b"a", [Key("a", "a")]),
    ("letter-Z", b"Z", [Key("Z", "Z")]),
    ("symbol-slash", b"/", [Key("/", "/")]),
    ("batch-text", b"ab", [Key("a", "a"), Key("b", "b")]),
    # --- Alt prefixes (spec 1.7) -----------------------------------------
    ("alt-backspace", b"\x1b\x7f", [Key("alt+backspace")]),
    ("alt-enter", b"\x1b\r", [Key("alt+enter")]),
    ("alt-tab", b"\x1b\t", [Key("alt+tab")]),
    ("alt-space", b"\x1b ", [Key("alt+space")]),
    ("alt-left-alias", b"\x1bB", [Key("alt+left")]),
    ("alt-right-alias", b"\x1bF", [Key("alt+right")]),
    ("alt-x", b"\x1bx", [Key("alt+x")]),
    ("alt-shift-p", b"\x1bP", [Key("alt+shift+p")]),
    ("ctrl-alt-p", b"\x1b\x10", [Key("ctrl+alt+p")]),
    ("meta-csi", b"\x1b\x1b[A", [Key("alt+up")]),
    ("meta-ss3", b"\x1b\x1bOP", [Key("alt+f1")]),
    ("esc-then-alt", b"\x1b\x1bX", [Key("escape"), Key("alt+shift+x")]),
    ("shift-tab", b"\x1b[Z", [Key("shift+tab")]),
    ("keypad-enter", b"\x1bOM", [Key("enter")]),
    # --- kitty CSI-u (spec 1.9) ------------------------------------------
    ("kitty-plain-a", b"\x1b[97u", [Key("a", "a")]),
    ("kitty-ctrl-a", b"\x1b[97;5u", [Key("ctrl+a")]),
    ("kitty-shift-a", b"\x1b[97;2u", [Key("shift+a", "a")]),
    ("kitty-shift-tab", b"\x1b[9;2u", [Key("shift+tab")]),
    ("kitty-shift-enter", b"\x1b[13;2u", [Key("shift+enter")]),
    ("kitty-ctrl-enter", b"\x1b[13;5u", [Key("ctrl+enter")]),
    ("kitty-alt-enter", b"\x1b[13;3u", [Key("alt+enter")]),
    ("kitty-super-alt-backspace", b"\x1b[127;11u", [Key("alt+super+backspace")]),
    ("kitty-shift-super-a", b"\x1b[97;10u", [Key("shift+super+a")]),
    ("kitty-cyrillic-ctrl-c", b"\x1b[1089::99;5u", [Key("ctrl+c")]),
    ("kitty-baselayout-letter", b"\x1b[108::97;5u", [Key("ctrl+l")]),
    ("kitty-keypad-1", b"\x1b[57400u", [Key("1", "1")]),
    ("kitty-keypad-1-numlock", b"\x1b[57400;129u", [Key("1", "1")]),
    ("kitty-keypad-ctrl-end", b"\x1b[57400;133u", [Key("ctrl+end")]),
    ("kitty-keypad-divide", b"\x1b[57410u", [Key("/", "/")]),
    ("kitty-keypad-ctrl-plus", b"\x1b[57413;5u", [Key("ctrl++")]),
    ("kitty-repeat-backspace", b"\x1b[127;1:2u", [Key("backspace")]),
    ("kitty-release-unknown", b"\x1b[127;1:3u", [Key("unknown", "\x1b[127;1:3u")]),
    ("kitty-hyper-rejected", b"\x1b[99;17u", [Key("unknown", "\x1b[99;17u")]),
    ("kitty-emoji", b"\x1b[128512u", [Key("\U0001f600", "\U0001f600")]),
    # --- CSI 1;mod <letter> (spec 1.10) ----------------------------------
    ("csi-1-letter-ctrl-right", b"\x1b[1;5C", [Key("ctrl+right")]),
    ("csi-1-letter-alt-up", b"\x1b[1;3A", [Key("alt+up")]),
    ("csi-1-letter-shift-ctrl-left", b"\x1b[1;6D", [Key("shift+ctrl+left")]),
    ("csi-1-letter-shift-f1", b"\x1b[1;2P", [Key("shift+f1")]),
    ("csi-1-letter-shift-clear", b"\x1b[1;2E", [Key("shift+clear")]),
    # --- CSI <n>~ functional with modifiers (spec 1.11) ------------------
    ("func-shift-delete", b"\x1b[3;2~", [Key("shift+delete")]),
    ("func-ctrl-f5", b"\x1b[15;5~", [Key("ctrl+f5")]),
    ("func-shift-end", b"\x1b[4;2~", [Key("shift+end")]),
    ("func-shift-f3", b"\x1b[13;2~", [Key("shift+f3")]),
    # --- xterm modifyOtherKeys (spec 1.12) -------------------------------
    ("mok-plain-a", b"\x1b[27;1;97~", [Key("a", "a")]),
    ("mok-shift-a", b"\x1b[27;2;97~", [Key("shift+a", "a")]),
    ("mok-ctrl-a", b"\x1b[27;5;97~", [Key("ctrl+a")]),
    ("mok-ctrl-alt-a", b"\x1b[27;7;97~", [Key("ctrl+alt+a")]),
    ("mok-ctrl-m", b"\x1b[27;5;109~", [Key("ctrl+m")]),
    ("mok-no-tilde", b"\x1b[27;5;109", [Key("ctrl+m")]),
    # --- UTF-8 (spec 1.14) -----------------------------------------------
    ("utf8-latin1", b"\xc3\xa9", [Key("\xe9", "\xe9")]),
    ("utf8-cjk", b"\xe4\xbd\xa0\xe5\xa5\xbd", [Key("\u4f60", "\u4f60"), Key("\u597d", "\u597d")]),
    ("utf8-emoji", b"\xf0\x9f\x98\x80", [Key("\U0001f600", "\U0001f600")]),
    ("utf8-invalid", b"\xff", [Key("unknown", "\ufffd")]),
    ("utf8-overlong", b"\xc0\xaf", [Key("unknown", "\ufffd"), Key("unknown", "\ufffd")]),
    ("utf8-bad-continuation", b"\xe0\x28", [Key("unknown", "\ufffd"), Key("(", "(")]),
    # --- bracketed paste (spec 2) ----------------------------------------
    ("paste-basic", b"\x1b[200~hello\x1b[201~", [Key("paste", "hello")]),
    ("paste-empty", b"\x1b[200~\x1b[201~", [Key("paste", "")]),
    ("paste-controls", b"\x1b[200~a\x03b\x1b[cd\x1b[201~", [Key("paste", "a\x03b\x1b[cd")]),
    ("paste-utf8", b"\x1b[200~caf\xc3\xa9\x1b[201~", [Key("paste", "caf\xe9")]),
    ("paste-then-enter", b"\x1b[200~x\x1b[201~\r", [Key("paste", "x"), Key("enter")]),
    ("paste-unterminated", b"\x1b[200~abc", [Key("paste", "abc")]),
    ("paste-mode-enable", b"\x1b[?2004h", [Key("unknown", "\x1b[?2004h")]),
    ("paste-mode-disable", b"\x1b[?2004l", [Key("unknown", "\x1b[?2004l")]),
    ("stray-paste-end", b"\x1b[201~", [Key("unknown", "\x1b[201~")]),
    # --- unrecognised input ----------------------------------------------
    ("mouse-sgr", b"\x1b[<35;20;5M", [Key("unknown", "\x1b[<35;20;5M")]),
    ("kitty-probe-reply", b"\x1b[?1u", [Key("unknown", "\x1b[?1u")]),
    ("esc-slash", b"\x1b/", [Key("unknown", "\x1b/")]),
    ("partial-csi", b"\x1b[1;5", [Key("unknown", "\x1b[1;5")]),
    ("mixed", b"a\x1b[Ab", [Key("a", "a"), Key("up"), Key("b", "b")]),
]

CORPUS_IDS = [case[0] for case in CORPUS]


@pytest.mark.parametrize(("data", "expected"), [(c[1], c[2]) for c in CORPUS], ids=CORPUS_IDS)
def test_sequence_corpus(data: bytes, expected: list[Key]) -> None:
    assert _decode(data) == expected


def test_byte_at_a_time_matches_one_shot() -> None:
    for name, data, expected in CORPUS:
        decoder = KeyDecoder()
        streamed: list[Key] = []
        for index in range(len(data)):
            streamed.extend(decoder.feed(data[index : index + 1]))
        streamed.extend(decoder.flush())
        assert streamed == expected, name


def test_pasted_control_bytes_stay_inside_the_payload() -> None:
    payload = b"\x03\x1b[A\x7f\x1b[2~"
    keys = _decode(b"\x1b[200~" + payload + b"\x1b[201~")
    assert keys == [Key("paste", payload.decode("utf-8"))]


def test_paste_markers_may_split_across_feeds() -> None:
    decoder = KeyDecoder()
    assert decoder.feed(b"\x1b[20") == []
    assert decoder.feed(b"0~hi\x1b[20") == []
    assert decoder.feed(b"1~") == [Key("paste", "hi")]
    assert decoder.feed(b"") == []


def test_split_multibyte_utf8() -> None:
    decoder = KeyDecoder()
    assert decoder.feed(b"\xe4\xbd") == []
    assert decoder.feed(b"\xa0") == [Key("\u4f60", "\u4f60")]
    assert decoder.feed(b"\xf0\x9f") == []
    assert decoder.feed(b"\x98\x80") == [Key("\U0001f600", "\U0001f600")]


def test_split_escape_sequences() -> None:
    decoder = KeyDecoder()
    assert decoder.feed(b"\x1b") == []
    assert decoder.feed(b"[") == []
    assert decoder.feed(b"1;5") == []
    assert decoder.feed(b"C") == [Key("ctrl+right")]
    assert decoder.feed(b"\x1b[") == []
    assert decoder.feed(b"A") == [Key("up")]


def test_flush_resolves_a_trailing_partial() -> None:
    decoder = KeyDecoder()
    assert decoder.feed(b"\x1b") == []
    assert decoder.flush() == [Key("escape")]
    # A decoder stays usable after a flush.
    assert decoder.feed(b"a") == [Key("a", "a")]

    decoder = KeyDecoder()
    assert decoder.feed(b"\x1b[") == []
    assert decoder.flush() == [Key("unknown", "\x1b[")]

    decoder = KeyDecoder()
    assert decoder.feed(b"\x1b\x1b") == []
    assert decoder.flush() == [Key("escape"), Key("escape")]

    decoder = KeyDecoder()
    assert decoder.feed(b"\xc3") == []
    assert decoder.flush() == [Key("unknown", "\ufffd")]

    assert KeyDecoder().flush() == []


def test_kitty_active_switches_uppercase_meta_b_f() -> None:
    assert _decode(b"\x1bB") == [Key("alt+left")]
    assert _decode(b"\x1bF") == [Key("alt+right")]
    assert _decode(b"\x1bB", kitty_active=True) == [Key("alt+shift+b")]
    assert _decode(b"\x1bF", kitty_active=True) == [Key("alt+shift+f")]


def test_decoders_do_not_share_state() -> None:
    first = KeyDecoder()
    second = KeyDecoder()
    assert first.feed(b"\x1b[20") == []
    assert second.feed(b"a") == [Key("a", "a")]
    assert first.feed(b"0~x\x1b[201~") == [Key("paste", "x")]
