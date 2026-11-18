"""Strictness of the shared suspend-by-unwind park surface."""

from __future__ import annotations

import pytest

from voidcode.sdk.suspend import ParkError, ParkIdentity, ParkRevisionError, SuspendPark


def test_park_identity_round_trip() -> None:
    identity = ParkIdentity(park_id="park-1", session_id="s-1", revision_id="rev-1", owner="eval")
    assert ParkIdentity.from_payload(identity.as_payload()) == identity


@pytest.mark.parametrize("field", ["park_id", "session_id", "revision_id", "owner"])
@pytest.mark.parametrize("bad", ["", None, 0, b"x"])
def test_park_identity_rejects_blank_ids(field: str, bad: object) -> None:
    kwargs: dict[str, object] = {"park_id": "p", "session_id": "s", "revision_id": "rev-1", "owner": "eval"}
    kwargs[field] = bad
    with pytest.raises(ValueError):
        ParkIdentity(**kwargs)  # type: ignore[arg-type]


def test_park_identity_from_payload_rejects_unknown_missing_and_non_object() -> None:
    good = {"park_id": "p", "session_id": "s", "revision_id": "r", "owner": "o"}
    with pytest.raises(ValueError, match="unknown"):
        ParkIdentity.from_payload({**good, "extra": 1})
    with pytest.raises(ValueError, match="missing"):
        ParkIdentity.from_payload({"park_id": "p"})
    with pytest.raises(ValueError, match="object"):
        ParkIdentity.from_payload([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        ParkIdentity.from_payload({**good, "owner": ""})


def test_suspend_park_carries_owned_json() -> None:
    intent = {"draft": {"cell_source": "1 + 1"}, "tags": ["a"]}
    park = SuspendPark("composition", intent)
    assert park.park_kind == "composition"
    intent["draft"]["cell_source"] = "mutated"  # type: ignore[index]
    intent["tags"].append("b")  # type: ignore[union-attr]
    assert park.intent == {"draft": {"cell_source": "1 + 1"}, "tags": ("a",)}
    with pytest.raises(ValueError):
        SuspendPark("ask", {"bad": object()})
    with pytest.raises(ValueError, match="unknown park kind"):
        SuspendPark("other", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="JSON object"):
        SuspendPark("ask", [])  # type: ignore[arg-type]


def test_suspend_park_is_base_exception_not_exception() -> None:
    park = SuspendPark("ask", {"draft": {}})
    assert isinstance(park, BaseException)
    assert not isinstance(park, Exception)
    caught: str | None = None
    try:
        raise park
    except Exception:
        caught = "exception"
    except SuspendPark as exc:
        caught = exc.park_kind
    assert caught == "ask"


def test_park_errors_are_plain_values() -> None:
    assert issubclass(ParkError, ValueError)
    assert issubclass(ParkRevisionError, ValueError)
