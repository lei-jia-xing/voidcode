from __future__ import annotations

import pytest

from voidcode.runtime.contracts import BackgroundTaskConcurrencySnapshot


def test_concurrency_snapshot_as_payload() -> None:
    snapshot = BackgroundTaskConcurrencySnapshot(
        provider="openai",
        model="gpt-4",
        limit=3,
        limit_source="model",
        running_provider=1,
        running_model=1,
        running_total=2,
        queued_provider=2,
        queued_model=1,
        queued_total=5,
    )

    payload = snapshot.as_payload()

    assert payload == {
        "provider": "openai",
        "model": "gpt-4",
        "limit": 3,
        "limit_source": "model",
        "running_provider": 1,
        "running_model": 1,
        "running_total": 2,
        "queued_provider": 2,
        "queued_model": 1,
        "queued_total": 5,
    }


def test_concurrency_snapshot_is_frozen() -> None:
    snapshot = BackgroundTaskConcurrencySnapshot(
        provider="openai",
        model="gpt-4",
        limit=3,
        limit_source="model",
        running_provider=0,
        running_model=0,
        running_total=0,
        queued_provider=0,
        queued_model=0,
        queued_total=0,
    )

    with pytest.raises(AttributeError):
        snapshot.provider = "anthropic"


def test_concurrency_snapshot_serializes_all_fields() -> None:
    snapshot = BackgroundTaskConcurrencySnapshot(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        limit=5,
        limit_source="default",
        running_provider=0,
        running_model=0,
        running_total=0,
        queued_provider=0,
        queued_model=0,
        queued_total=0,
    )

    payload = snapshot.as_payload()
    expected_keys = {
        "provider",
        "model",
        "limit",
        "limit_source",
        "running_provider",
        "running_model",
        "running_total",
        "queued_provider",
        "queued_model",
        "queued_total",
    }
    assert set(payload.keys()) == expected_keys


def test_concurrency_snapshot_with_high_counts() -> None:
    snapshot = BackgroundTaskConcurrencySnapshot(
        provider="openai",
        model="gpt-4",
        limit=10,
        limit_source="provider",
        running_provider=10,
        running_model=8,
        running_total=15,
        queued_provider=20,
        queued_model=12,
        queued_total=50,
    )

    payload = snapshot.as_payload()
    assert payload["running_provider"] == 10
    assert payload["running_total"] == 15
    assert payload["queued_total"] == 50
    assert payload["limit_source"] == "provider"


def test_concurrency_snapshot_none_state() -> None:
    snapshot = BackgroundTaskConcurrencySnapshot(
        provider="none",
        model="none",
        limit=5,
        limit_source="default",
        running_provider=0,
        running_model=0,
        running_total=0,
        queued_provider=0,
        queued_model=0,
        queued_total=0,
    )

    payload = snapshot.as_payload()
    assert payload["provider"] == "none"
    assert payload["running_total"] == 0
    assert payload["queued_total"] == 0
