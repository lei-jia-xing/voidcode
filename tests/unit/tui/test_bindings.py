"""``config.tui.keymap`` parsing: configured string -> the decoder's ``Key``.

The parser must speak exactly the canonical names :class:`~voidcode.tui.keys.KeyDecoder`
emits, so the drift guard below feeds the decoder the raw bytes of the same
physical key and compares names.
"""

from __future__ import annotations

import pytest

from voidcode.tui.app import KeyBindingError, parse_key_binding, parse_keymap
from voidcode.tui.keys import KeyDecoder
from voidcode.tui.transcript import KeyHints


def _decode(raw: bytes) -> str:
    decoder = KeyDecoder()
    # A lone ``ESC`` is buffered until it is known not to start a sequence.
    keys = decoder.feed(raw) or decoder.flush()
    assert len(keys) == 1, keys
    return keys[0].name


@pytest.mark.parametrize(
    ("spec", "raw"),
    [
        ("ctrl+o", b"\x0f"),
        ("ctrl+s", b"\x13"),
        ("ctrl+c", b"\x03"),
        ("escape", b"\x1b"),
        ("enter", b"\r"),
        ("tab", b"\t"),
        ("space", b" "),
        ("backspace", b"\x7f"),
        ("delete", b"\x1b[3~"),
        ("up", b"\x1b[A"),
        ("pageUp", b"\x1b[5~"),
        ("home", b"\x1bOH"),
        ("f5", b"\x1b[15~"),
        ("alt+up", b"\x1b[1;3A"),
        ("shift+up", b"\x1b[1;2A"),
        ("shift+delete", b"\x1b[3$"),
        ("o", b"o"),
        ("/", b"/"),
    ],
)
def test_parsed_name_matches_the_decoder_canonical_name(spec: str, raw: bytes) -> None:
    assert parse_key_binding(spec).name == _decode(raw)


def test_known_aliases_normalise_to_the_canonical_spelling() -> None:
    assert parse_key_binding("esc").name == "escape"
    assert parse_key_binding("Esc").name == "escape"
    assert parse_key_binding("CTRL+O").name == "ctrl+o"
    assert parse_key_binding("return").name == "enter"
    assert parse_key_binding("pgdn").name == "pageDown"
    # Canonical modifier order matches the decoder's (shift, ctrl, alt, super).
    assert parse_key_binding("alt+ctrl+up").name == "ctrl+alt+up"
    assert parse_key_binding("super+shift+k").name == "shift+super+k"


@pytest.mark.parametrize(
    "spec",
    [
        "ctrl+nosuchkey",
        "hyper+o",
        "ctrl+",
        "ctrl++o",
        "+o",
        "ctrl+ctrl+o",
        "ctrl",
        "",
        "   ",
    ],
)
def test_unknown_key_strings_fail_loudly(spec: str) -> None:
    with pytest.raises(KeyBindingError):
        parse_key_binding(spec)


def test_non_string_binding_fails_loudly() -> None:
    with pytest.raises(KeyBindingError):
        parse_key_binding(None)  # type: ignore[arg-type]


def test_default_keymap_binds_only_the_expand_key() -> None:
    bindings = parse_keymap(None)
    assert {action: key.name for action, key in bindings.items()} == {"app.tools.expand": "ctrl+o"}


def test_configured_keymap_maps_key_chords_to_actions() -> None:
    """The config direction is chord -> action (``config.tui.keymap``)."""
    bindings = parse_keymap({"ctrl+t": "app.tools.expand", "ctrl+n": "app.session.new", "ctrl+r": "app.session.resume"})
    assert {action: key.name for action, key in bindings.items()} == {
        "app.tools.expand": "ctrl+t",
        "app.session.new": "ctrl+n",
        "app.session.resume": "ctrl+r",
    }


def test_unknown_action_fails_loudly() -> None:
    with pytest.raises(KeyBindingError, match="unknown action 'session_kill'"):
        parse_keymap({"ctrl+k": "session_kill"})


def test_unknown_key_string_in_the_keymap_fails_loudly() -> None:
    with pytest.raises(KeyBindingError, match="unknown key"):
        parse_keymap({"ctrl+nosuchkey": "app.tools.expand"})


def test_rebound_expand_key_reaches_the_hint_text() -> None:
    """The hint table is built from the resolved binding, so hints stay truthful."""
    bindings = parse_keymap({"ctrl+t": "app.tools.expand"})
    assert KeyHints(expand=bindings["app.tools.expand"].name).key_label() == "Ctrl+T"
