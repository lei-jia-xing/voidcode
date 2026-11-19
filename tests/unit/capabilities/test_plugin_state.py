"""Unit tests for the shared plugin session-state store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from voidcode.capabilities.plugin_state import PluginStateStore, require_keys


@dataclass(frozen=True, slots=True)
class _Note:
    text: str = ""
    revision: int = 0

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> _Note:
        text = raw["text"]
        revision = raw["revision"]
        assert isinstance(text, str) and isinstance(revision, int)
        return cls(text=text, revision=revision)

    def serialize(self) -> dict[str, object]:
        return {"text": self.text, "revision": self.revision}


class _StubServices:
    def __init__(self, payload: Mapping[str, object] | None = None, version: int = 0) -> None:
        self.payload = payload
        self.version = version
        self.saved: list[tuple[dict[str, object], int]] = []

    def get_state(self, scope: str = "session"):
        assert scope == "session"
        return self.payload, self.version

    def replace_state(self, state: Mapping[str, object], expected_version: int) -> int:
        if expected_version != self.version:
            raise ValueError(f"state conflict: expected {expected_version}, have {self.version}")
        self.version += 1
        self.payload = dict(state)
        self.saved.append((dict(state), expected_version))
        return self.version


def _store(services: _StubServices) -> PluginStateStore[_Note]:
    return PluginStateStore(
        services,  # type: ignore[arg-type]
        keys=frozenset({"text", "revision"}),
        parse=_Note.parse,
        serialize=_Note.serialize,
        empty=_Note,
        unknown=lambda extra: f"note state field '{extra[0]}' is not supported",
        missing=lambda absent: "note state is missing field(s): " + ", ".join(absent),
    )


def test_require_keys_accepts_exact_shape() -> None:
    require_keys(
        {"text": "a", "revision": 1},
        frozenset({"text", "revision"}),
        unknown=lambda extra: f"note state field '{extra[0]}' is not supported",
        missing=lambda absent: "note state is missing field(s): " + ", ".join(absent),
    )


def test_require_keys_rejects_first_sorted_unknown() -> None:
    with pytest.raises(ValueError, match="^note state field 'b' is not supported$"):
        require_keys(
            {"text": "a", "revision": 1, "z": 0, "b": 0},
            frozenset({"text", "revision"}),
            unknown=lambda extra: f"note state field '{extra[0]}' is not supported",
        )


def test_require_keys_rejects_missing_as_joined_list() -> None:
    with pytest.raises(ValueError, match="^note state is missing field\\(s\\): revision, text$"):
        require_keys(
            {},
            frozenset({"text", "revision"}),
            unknown=lambda extra: f"note state field '{extra[0]}' is not supported",
            missing=lambda absent: "note state is missing field(s): " + ", ".join(absent),
        )


def test_require_keys_skips_missing_check_when_unasked() -> None:
    require_keys(
        {},
        frozenset({"text", "revision"}),
        unknown=lambda extra: f"note state field '{extra[0]}' is not supported",
    )


def test_load_returns_empty_state_for_absent_payload() -> None:
    state, version = _store(_StubServices(None, 0)).load()
    assert (state, version) == (_Note(), 0)


def test_load_round_trips_payload_and_version() -> None:
    state, version = _store(_StubServices({"text": "hi", "revision": 3}, 7)).load()
    assert (state, version) == (_Note(text="hi", revision=3), 7)


def test_load_surfaces_plugin_messages_verbatim() -> None:
    with pytest.raises(ValueError, match="^note state field 'b' is not supported$"):
        _store(_StubServices({"text": "a", "revision": 1, "b": 0}, 0)).load()
    with pytest.raises(ValueError, match="^note state is missing field\\(s\\): revision$"):
        _store(_StubServices({"text": "a"}, 0)).load()


def test_save_serializes_under_read_version_and_returns_next() -> None:
    services = _StubServices({"text": "hi", "revision": 3}, 7)
    store = _store(services)
    state, version = store.load()
    assert store.save(_Note(text="yo", revision=state.revision + 1), version) == 8
    assert services.saved == [({"text": "yo", "revision": 4}, 7)]


def test_save_conflict_surfaces_loudly() -> None:
    services = _StubServices({"text": "hi", "revision": 3}, 7)
    with pytest.raises(ValueError, match="state conflict"):
        _store(services).save(_Note(), 6)
    assert services.saved == []
