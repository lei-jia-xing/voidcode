from __future__ import annotations

import pytest

from voidcode.runtime.contracts import (
    RuntimeRequestError,
    runtime_subagent_routing_from_metadata,
    validate_runtime_subagent_routing_metadata,
)
from voidcode.runtime.task import subagent_routing_identity_from_metadata


def test_request_and_task_metadata_share_delegation_identity_parser() -> None:
    metadata: dict[str, object] = {
        "delegation": {
            "mode": "background",
            "category": "quick",
            "description": "Inspect the runtime",
            "depth": 2,
        }
    }

    assert runtime_subagent_routing_from_metadata(metadata) == subagent_routing_identity_from_metadata(metadata)


@pytest.mark.parametrize(
    "delegation",
    [
        {"mode": "invalid", "category": "quick"},
        {"mode": "sync", "category": "quick", "subagent_type": "worker"},
        {"mode": "sync", "category": ""},
    ],
)
def test_request_and_task_metadata_reject_the_same_invalid_identity(
    delegation: dict[str, object],
) -> None:
    metadata: dict[str, object] = {"delegation": delegation}

    with pytest.raises(ValueError):
        subagent_routing_identity_from_metadata(metadata)
    with pytest.raises(RuntimeRequestError):
        runtime_subagent_routing_from_metadata(metadata)


def test_request_boundary_still_rejects_unknown_delegation_fields() -> None:
    with pytest.raises(RuntimeRequestError, match="unsupported request metadata 'delegation'"):
        runtime_subagent_routing_from_metadata(
            {
                "delegation": {
                    "mode": "sync",
                    "category": "quick",
                    "unexpected": True,
                }
            }
        )


def test_parallel_group_fields_are_accepted_and_normalized() -> None:
    # Given: delegation metadata carrying the parallel-group fields the task tool emits
    delegation: dict[str, object] = {
        "mode": "background",
        "category": "worker",
        "parallel_group_id": "alpha-beta-writers",
        "parallel_group_size": "2",
    }

    # When: the request boundary normalizes it
    normalized = validate_runtime_subagent_routing_metadata(delegation)

    # Then: both fields survive, and the size is normalized to an integer
    assert normalized.get("parallel_group_id") == "alpha-beta-writers"
    assert normalized.get("parallel_group_size") == 2


def test_parallel_group_size_must_be_a_positive_integer() -> None:
    delegation: dict[str, object] = {
        "mode": "background",
        "category": "worker",
        "parallel_group_size": "0",
    }

    with pytest.raises(RuntimeRequestError, match="parallel_group_size"):
        validate_runtime_subagent_routing_metadata(delegation)
