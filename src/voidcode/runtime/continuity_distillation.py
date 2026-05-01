from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from ..tools.contracts import ToolResult

SourceReferenceKind = Literal["event", "tool", "session", "background_task"]
EvidenceKind = Literal["file", "command", "error", "other"]
VerificationStatus = Literal["passed", "failed", "pending", "unknown"]


@dataclass(frozen=True, slots=True)
class ContinuitySourceReference:
    kind: SourceReferenceKind
    id: str
    detail: str | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "id": self.id}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, slots=True)
class ContinuityDecisionFact:
    text: str
    rationale: str
    refs: tuple[ContinuitySourceReference, ...] = ()

    def metadata_payload(self) -> dict[str, object]:
        return {
            "text": self.text,
            "rationale": self.rationale,
            "refs": [ref.metadata_payload() for ref in self.refs],
        }


@dataclass(frozen=True, slots=True)
class ContinuityEvidenceFact:
    text: str
    kind: EvidenceKind
    refs: tuple[ContinuitySourceReference, ...] = ()

    def metadata_payload(self) -> dict[str, object]:
        return {
            "text": self.text,
            "kind": self.kind,
            "refs": [ref.metadata_payload() for ref in self.refs],
        }


@dataclass(frozen=True, slots=True)
class ContinuityVerificationState:
    status: VerificationStatus
    details: tuple[str, ...] = ()
    refs: tuple[ContinuitySourceReference, ...] = ()

    def metadata_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "details": list(self.details),
            "refs": [ref.metadata_payload() for ref in self.refs],
        }


@dataclass(frozen=True, slots=True)
class ContinuityDistillationRecord:
    objective_current_goal: str
    verbatim_user_constraints: tuple[str, ...]
    completed_progress: tuple[str, ...]
    blockers_open_questions: tuple[str, ...]
    key_decisions_with_rationale: tuple[ContinuityDecisionFact, ...]
    relevant_files_commands_errors: tuple[ContinuityEvidenceFact, ...]
    verification_state: ContinuityVerificationState
    next_steps: tuple[str, ...]
    source_references: tuple[ContinuitySourceReference, ...]

    def metadata_payload(self) -> dict[str, object]:
        return {
            "objective_current_goal": self.objective_current_goal,
            "verbatim_user_constraints": list(self.verbatim_user_constraints),
            "completed_progress": list(self.completed_progress),
            "blockers_open_questions": list(self.blockers_open_questions),
            "key_decisions_with_rationale": [
                item.metadata_payload() for item in self.key_decisions_with_rationale
            ],
            "relevant_files_commands_errors": [
                item.metadata_payload() for item in self.relevant_files_commands_errors
            ],
            "verification_state": self.verification_state.metadata_payload(),
            "next_steps": list(self.next_steps),
            "source_references": [ref.metadata_payload() for ref in self.source_references],
        }


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...] | None:
    raw = payload.get(key)
    if not isinstance(raw, list | tuple):
        return None
    values: list[str] = []
    for item in cast(list[object] | tuple[object, ...], raw):
        if not isinstance(item, str):
            return None
        stripped = item.strip()
        if stripped:
            values.append(stripped)
    return tuple(values)


def _source_references_from_payload(raw: object) -> tuple[ContinuitySourceReference, ...] | None:
    if not isinstance(raw, list | tuple):
        return None
    refs: list[ContinuitySourceReference] = []
    for item in cast(list[object] | tuple[object, ...], raw):
        if not isinstance(item, dict):
            return None
        entry = cast(dict[str, object], item)
        raw_kind = entry.get("kind")
        raw_id = entry.get("id")
        if raw_kind not in {"event", "tool", "session", "background_task"}:
            return None
        if not isinstance(raw_id, str) or not raw_id.strip():
            return None
        raw_detail = entry.get("detail")
        detail = raw_detail.strip() if isinstance(raw_detail, str) and raw_detail.strip() else None
        refs.append(
            ContinuitySourceReference(
                kind=cast(SourceReferenceKind, raw_kind),
                id=raw_id.strip(),
                detail=detail,
            )
        )
    return tuple(refs)


def _decisions_from_payload(raw: object) -> tuple[ContinuityDecisionFact, ...] | None:
    if not isinstance(raw, list | tuple):
        return None
    facts: list[ContinuityDecisionFact] = []
    for item in cast(list[object] | tuple[object, ...], raw):
        if not isinstance(item, dict):
            return None
        entry = cast(dict[str, object], item)
        text = entry.get("text")
        rationale = entry.get("rationale")
        refs = _source_references_from_payload(entry.get("refs"))
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(rationale, str) or not rationale.strip():
            return None
        if refs is None:
            return None
        facts.append(
            ContinuityDecisionFact(text=text.strip(), rationale=rationale.strip(), refs=refs)
        )
    return tuple(facts)


def _evidence_from_payload(raw: object) -> tuple[ContinuityEvidenceFact, ...] | None:
    if not isinstance(raw, list | tuple):
        return None
    facts: list[ContinuityEvidenceFact] = []
    for item in cast(list[object] | tuple[object, ...], raw):
        if not isinstance(item, dict):
            return None
        entry = cast(dict[str, object], item)
        text = entry.get("text")
        kind = entry.get("kind")
        refs = _source_references_from_payload(entry.get("refs"))
        if not isinstance(text, str) or not text.strip():
            return None
        if kind not in {"file", "command", "error", "other"}:
            return None
        if refs is None:
            return None
        facts.append(
            ContinuityEvidenceFact(
                text=text.strip(),
                kind=cast(EvidenceKind, kind),
                refs=refs,
            )
        )
    return tuple(facts)


def _verification_state_from_payload(raw: object) -> ContinuityVerificationState | None:
    if not isinstance(raw, dict):
        return None
    payload = cast(dict[str, object], raw)
    status = payload.get("status")
    if status not in {"passed", "failed", "pending", "unknown"}:
        return None
    details = _string_tuple(payload, "details")
    refs = _source_references_from_payload(payload.get("refs"))
    if details is None or refs is None:
        return None
    return ContinuityVerificationState(
        status=cast(VerificationStatus, status),
        details=details,
        refs=refs,
    )


def distillation_record_from_payload(
    raw_payload: Mapping[str, object],
) -> ContinuityDistillationRecord | None:
    objective = raw_payload.get("objective_current_goal")
    if not isinstance(objective, str) or not objective.strip():
        return None

    constraints = _string_tuple(raw_payload, "verbatim_user_constraints")
    progress = _string_tuple(raw_payload, "completed_progress")
    blockers = _string_tuple(raw_payload, "blockers_open_questions")
    decisions = _decisions_from_payload(raw_payload.get("key_decisions_with_rationale"))
    evidence = _evidence_from_payload(raw_payload.get("relevant_files_commands_errors"))
    verification = _verification_state_from_payload(raw_payload.get("verification_state"))
    next_steps = _string_tuple(raw_payload, "next_steps")
    refs = _source_references_from_payload(raw_payload.get("source_references"))

    if (
        constraints is None
        or progress is None
        or blockers is None
        or decisions is None
        or evidence is None
        or verification is None
        or next_steps is None
        or refs is None
    ):
        return None

    return ContinuityDistillationRecord(
        objective_current_goal=objective.strip(),
        verbatim_user_constraints=constraints,
        completed_progress=progress,
        blockers_open_questions=blockers,
        key_decisions_with_rationale=decisions,
        relevant_files_commands_errors=evidence,
        verification_state=verification,
        next_steps=next_steps,
        source_references=refs,
    )


def sanitize_distillation_text(value: str, *, max_chars: int) -> str:
    redacted = value.replace("data:", "[redacted-data-uri]:")
    if len(redacted) <= max_chars:
        return redacted
    return redacted[:max_chars]


def build_distillation_input_envelope(
    *,
    prompt: str,
    dropped_results: tuple[ToolResult, ...],
    retained_results: tuple[ToolResult, ...],
    previous_continuity: Mapping[str, object] | None,
    max_items: int,
    max_chars: int,
) -> dict[str, object]:
    def _source_refs(result: ToolResult) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        tool_call_id = result.data.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            refs.append({"kind": "tool", "id": tool_call_id.strip()})
        path = result.data.get("path")
        if isinstance(path, str) and path.strip():
            refs.append({"kind": "event", "id": f"file:{path.strip()}"})
        command = result.data.get("command")
        if isinstance(command, str) and command.strip():
            refs.append({"kind": "event", "id": f"command:{command.strip()}"})
        return refs

    def _preview_result(result: ToolResult) -> dict[str, object]:
        content = result.content or result.error or ""
        safe_content = sanitize_distillation_text(content, max_chars=max_chars)
        data = dict(result.data)
        for key in ("data_uri", "image_data", "raw_output", "stdout", "stderr"):
            if key in data:
                data[key] = "[redacted]"
        return {
            "tool_name": result.tool_name,
            "status": result.status,
            "content_preview": safe_content,
            "error_kind": result.error_kind,
            "truncated": result.truncated,
            "partial": result.partial,
            "data": data,
            "source_references": _source_refs(result),
        }

    return {
        "prompt": sanitize_distillation_text(prompt, max_chars=max_chars),
        "previous_continuity": dict(previous_continuity)
        if previous_continuity is not None
        else None,
        "dropped_tool_result_previews": [
            _preview_result(result) for result in dropped_results[:max_items]
        ],
        "recent_tail_previews": [
            _preview_result(result) for result in retained_results[-max_items:]
        ],
    }
