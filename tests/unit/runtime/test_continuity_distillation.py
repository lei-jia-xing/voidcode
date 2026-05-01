from __future__ import annotations

from typing import cast

from voidcode.runtime.continuity_distillation import (
    ContinuityDistillationRecord,
    build_distillation_input_envelope,
    distillation_record_from_payload,
)
from voidcode.tools.contracts import ToolResult


def _valid_payload() -> dict[str, object]:
    return {
        "objective_current_goal": "Implement model-assisted continuity distillation",
        "verbatim_user_constraints": ["Do not override explicit user instructions"],
        "completed_progress": ["Mapped continuity pipeline"],
        "blockers_open_questions": ["Need provider call integration"],
        "key_decisions_with_rationale": [
            {
                "text": "Keep deterministic fallback",
                "rationale": "Preserve existing reliability under failures",
                "refs": [
                    {
                        "kind": "event",
                        "id": "event:runtime.tool_completed:42",
                        "detail": "tool completed after compaction",
                    }
                ],
            }
        ],
        "relevant_files_commands_errors": [
            {
                "text": "src/voidcode/runtime/context_window.py",
                "kind": "file",
                "refs": [{"kind": "session", "id": "session:abc123"}],
            },
            {
                "text": "mise run test",
                "kind": "command",
                "refs": [{"kind": "tool", "id": "tool:bash:12"}],
            },
        ],
        "verification_state": {
            "status": "pending",
            "details": ["Unit tests not run yet"],
            "refs": [{"kind": "background_task", "id": "bg:task-7"}],
        },
        "next_steps": ["Integrate in context_window"],
        "source_references": [
            {"kind": "session", "id": "session:abc123"},
            {"kind": "tool", "id": "tool:read_file:5"},
        ],
    }


def test_distillation_record_from_payload_accepts_valid_schema() -> None:
    parsed = distillation_record_from_payload(_valid_payload())
    assert isinstance(parsed, ContinuityDistillationRecord)
    assert parsed.objective_current_goal == "Implement model-assisted continuity distillation"
    assert parsed.verification_state.status == "pending"
    assert parsed.relevant_files_commands_errors[0].kind == "file"


def test_distillation_record_from_payload_rejects_missing_required_field() -> None:
    payload = _valid_payload()
    payload.pop("source_references")
    assert distillation_record_from_payload(payload) is None


def test_distillation_record_from_payload_rejects_invalid_reference_kind() -> None:
    payload = _valid_payload()
    refs = payload["source_references"]
    assert isinstance(refs, list)
    refs[0] = {"kind": "unknown", "id": "x"}
    assert distillation_record_from_payload(payload) is None


def test_distillation_record_round_trip_payload_is_stable() -> None:
    parsed = distillation_record_from_payload(_valid_payload())
    assert parsed is not None
    meta = parsed.metadata_payload()
    reparsed = distillation_record_from_payload(meta)
    assert reparsed == parsed


def test_build_distillation_input_envelope_redacts_oversized_and_data_uri_fields() -> None:
    envelope = build_distillation_input_envelope(
        prompt="summarize context",
        dropped_results=(
            ToolResult(
                tool_name="read",
                status="ok",
                content="data:image/png;base64,AAAA" + ("x" * 6000),
                data={"data_uri": "data:image/png;base64,AAAA", "stdout": "secret-log"},
            ),
        ),
        retained_results=(),
        previous_continuity=None,
        max_items=2,
        max_chars=120,
    )

    dropped_raw = envelope["dropped_tool_result_previews"]
    assert isinstance(dropped_raw, list)
    dropped = cast(list[object], dropped_raw)
    item = dropped[0]
    assert isinstance(item, dict)
    item_dict = cast(dict[str, object], item)
    content_preview = item_dict["content_preview"]
    assert isinstance(content_preview, str)
    assert len(content_preview) <= 120
    assert "data:image" not in content_preview
    data = item_dict["data"]
    assert isinstance(data, dict)
    assert data["data_uri"] == "[redacted]"
    assert data["stdout"] == "[redacted]"
    refs = item_dict["source_references"]
    assert isinstance(refs, list)


def test_build_distillation_input_envelope_collects_source_references() -> None:
    envelope = build_distillation_input_envelope(
        prompt="summarize",
        dropped_results=(
            ToolResult(
                tool_name="bash",
                status="ok",
                content="done",
                data={"tool_call_id": "call-1", "path": "src/a.py", "command": "pytest"},
            ),
        ),
        retained_results=(),
        previous_continuity=None,
        max_items=3,
        max_chars=200,
    )

    dropped_raw = envelope["dropped_tool_result_previews"]
    assert isinstance(dropped_raw, list)
    preview = cast(dict[str, object], dropped_raw[0])
    refs = preview["source_references"]
    assert isinstance(refs, list)
    typed_refs = cast(list[dict[str, str]], refs)
    assert {item["id"] for item in typed_refs} == {
        "call-1",
        "file:src/a.py",
        "command:pytest",
    }
