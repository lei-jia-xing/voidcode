from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from ..tools.contracts import ToolResult

SourceReferenceKind = Literal["event", "tool", "session", "background_task"]
EvidenceKind = Literal["file", "command", "error", "other"]
VerificationStatus = Literal["passed", "failed", "pending", "unknown"]

_DISTILLATION_OUTPUT_MAX_TEXT_CHARS = 1_000
_DISTILLATION_OUTPUT_MAX_ITEMS = 12
_DISTILLATION_OUTPUT_MAX_REFERENCES = 24


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
    for item in cast(list[object] | tuple[object, ...], raw)[:_DISTILLATION_OUTPUT_MAX_ITEMS]:
        if not isinstance(item, str):
            return None
        stripped = _bounded_output_text(item)
        if stripped:
            values.append(stripped)
    return tuple(values)


def _source_references_from_payload(raw: object) -> tuple[ContinuitySourceReference, ...] | None:
    if not isinstance(raw, list | tuple):
        return None
    refs: list[ContinuitySourceReference] = []
    for item in cast(list[object] | tuple[object, ...], raw)[:_DISTILLATION_OUTPUT_MAX_REFERENCES]:
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
        detail = (
            _bounded_output_text(raw_detail)
            if isinstance(raw_detail, str) and raw_detail.strip()
            else None
        )
        refs.append(
            ContinuitySourceReference(
                kind=cast(SourceReferenceKind, raw_kind),
                id=_bounded_output_text(raw_id),
                detail=detail,
            )
        )
    return tuple(refs)


def _decisions_from_payload(raw: object) -> tuple[ContinuityDecisionFact, ...] | None:
    if not isinstance(raw, list | tuple):
        return None
    facts: list[ContinuityDecisionFact] = []
    for item in cast(list[object] | tuple[object, ...], raw)[:_DISTILLATION_OUTPUT_MAX_ITEMS]:
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
            ContinuityDecisionFact(
                text=_bounded_output_text(text),
                rationale=_bounded_output_text(rationale),
                refs=refs,
            )
        )
    return tuple(facts)


def _evidence_from_payload(raw: object) -> tuple[ContinuityEvidenceFact, ...] | None:
    if not isinstance(raw, list | tuple):
        return None
    facts: list[ContinuityEvidenceFact] = []
    for item in cast(list[object] | tuple[object, ...], raw)[:_DISTILLATION_OUTPUT_MAX_ITEMS]:
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
                text=_bounded_output_text(text),
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
        objective_current_goal=_bounded_output_text(objective),
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


def _bounded_output_text(value: str) -> str:
    return sanitize_distillation_text(
        value.strip(),
        max_chars=_DISTILLATION_OUTPUT_MAX_TEXT_CHARS,
    )


def build_distillation_input_envelope(
    *,
    prompt: str,
    dropped_results: tuple[ToolResult, ...],
    retained_results: tuple[ToolResult, ...],
    previous_continuity: Mapping[str, object] | None,
    max_items: int,
    max_chars: int,
) -> dict[str, object]:
    max_collection_items = max(1, max_items)

    def _bounded_payload(value: object, *, depth: int = 0) -> object:
        if depth >= 4:
            return "[truncated-depth]"
        if isinstance(value, str):
            return sanitize_distillation_text(value, max_chars=max_chars)
        if isinstance(value, dict):
            raw_mapping = cast(dict[object, object], value)
            bounded: dict[str, object] = {}
            for index, (raw_key, raw_value) in enumerate(raw_mapping.items()):
                if index >= max_collection_items:
                    bounded["__truncated_items__"] = len(raw_mapping) - max_collection_items
                    break
                key = str(raw_key)
                bounded[key] = _bounded_payload(raw_value, depth=depth + 1)
            return bounded
        if isinstance(value, list | tuple):
            source = cast(list[object] | tuple[object, ...], value)
            bounded_items = [
                _bounded_payload(item, depth=depth + 1) for item in source[:max_collection_items]
            ]
            if len(source) > max_collection_items:
                bounded_items.append({"__truncated_items__": len(source) - max_collection_items})
            return bounded_items
        if isinstance(value, bool | int | float) or value is None:
            return value
        return sanitize_distillation_text(str(value), max_chars=max_chars)

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
        bounded_data = _bounded_payload(data)
        assert isinstance(bounded_data, dict)
        return {
            "tool_name": result.tool_name,
            "status": result.status,
            "content_preview": safe_content,
            "error_kind": result.error_kind,
            "truncated": result.truncated,
            "partial": result.partial,
            "data": bounded_data,
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
