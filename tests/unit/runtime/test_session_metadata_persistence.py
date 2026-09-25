from __future__ import annotations

from typing import cast

import pytest

from voidcode.runtime.session import session_metadata_for_persistence


def _nested(mapping: object, *keys: str) -> object:
    current = mapping
    for key in keys:
        assert isinstance(current, dict)
        current = current[key]
    return current


@pytest.mark.parametrize(
    ("raw_summary", "expected_summary"),
    [
        # A long summary is resume truth: it is bounded by compaction and must not
        # be cut by the metadata length cap.
        pytest.param("S" * 1500, "S" * 1500, id="long-summary-not-truncated"),
        # Redaction still applies inside the same field.
        pytest.param("S" * 1200 + " sk-abcdef123456", "S" * 1200 + " <redacted>", id="secret-still-redacted"),
    ],
)
def test_summary_text_is_redacted_but_never_truncated(raw_summary: str, expected_summary: str) -> None:
    metadata: dict[str, object] = {
        "context_window": {"projection": {"summary_text": raw_summary}},
        "runtime_state": {"context_projection": {"summary_text": raw_summary}},
    }

    persisted = session_metadata_for_persistence(metadata)

    assert _nested(persisted, "context_window", "projection", "summary_text") == expected_summary
    assert _nested(persisted, "runtime_state", "context_projection", "summary_text") == expected_summary

    # Resume reads the persisted shape back through the same bound.
    assert session_metadata_for_persistence(persisted) == persisted


def test_non_summary_long_strings_keep_the_persistence_cap() -> None:
    metadata: dict[str, object] = {
        "tool_output": "T" * 1500,
        "context_window": {"projection": {"objective": "O" * 1500}},
    }

    persisted = session_metadata_for_persistence(metadata)

    tool_output = cast(str, persisted["tool_output"])
    assert tool_output.startswith("T" * 1000)
    assert "kept first 1000 of 1500 chars" in tool_output

    objective = cast(str, _nested(persisted, "context_window", "projection", "objective"))
    assert objective.startswith("O" * 1000)
    assert "kept first 1000 of 1500 chars" in objective
