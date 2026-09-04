from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .routing import SubagentRoutingIdentity, subagent_routing_identity_from_metadata


@dataclass(frozen=True, slots=True)
class SubagentExecutionCorrelation:
    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    child_session_id: str | None = None
    delegated_task_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentExecutionContract:
    correlation: SubagentExecutionCorrelation
    routing: SubagentRoutingIdentity | None = None

    @classmethod
    def from_snapshot(
        cls,
        *,
        parent_session_id: str | None,
        requested_child_session_id: str | None,
        child_session_id: str | None,
        delegated_task_id: str | None,
        metadata: Mapping[str, object] | None = None,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
    ) -> SubagentExecutionContract:
        return cls(
            correlation=SubagentExecutionCorrelation(
                parent_session_id=parent_session_id,
                requested_child_session_id=requested_child_session_id,
                child_session_id=child_session_id,
                delegated_task_id=delegated_task_id,
                approval_request_id=approval_request_id,
                question_request_id=question_request_id,
            ),
            routing=subagent_routing_identity_from_metadata(metadata),
        )
