from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.bundle import (
    SESSION_BUNDLE_REDACTED_PLACEHOLDER,
    SESSION_BUNDLE_SCHEMA_NAME,
    SessionBundleError,
    SessionBundleOptions,
    apply_session_bundle,
    build_session_bundle,
    parse_session_bundle,
    read_session_bundle_bytes,
    serialize_session_bundle,
)
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.tools import ToolResult, cap_tool_result_output


def _save_sample_session(tmp_path: Path, *, session_id: str = "bundle-session") -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(
        prompt="inspect failure with api_key=prompt-secret",
        session_id=session_id,
    )
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id=session_id),
            status="completed",
            turn=1,
            metadata={
                "runtime_state": {
                    "run_id": "run-1",
                    "api_key": "sk-test-secret",
                    "todos": {
                        "version": 1,
                        "revision": 1,
                        "todos": [],
                        "summary": {
                            "total": 0,
                            "pending": 0,
                            "in_progress": 0,
                            "completed": 0,
                            "cancelled": 0,
                            "active": 0,
                        },
                    },
                }
            },
        ),
        events=(
            EventEnvelope(
                session_id=session_id,
                sequence=1,
                event_type="runtime.tool_completed",
                source="runtime",
                payload={
                    "tool": "shell_exec",
                    "stdout": "x" * 2200,
                    "authorization": "Bearer abcdef123456",
                    "reasoning": "private chain of thought",
                },
            ),
            EventEnvelope(
                session_id=session_id,
                sequence=2,
                event_type="provider.raw_message",
                source="runtime",
                payload={"messages": [{"content": "raw provider content"}]},
            ),
            EventEnvelope(
                session_id=session_id,
                sequence=3,
                event_type="graph.response_ready",
                source="graph",
                payload={"summary": "done"},
            ),
        ),
        output="done with sk-output-secret",
    )
    store.save_run(workspace=tmp_path, request=request, response=response)


def _save_background_task_with_secrets(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id="secret-task"),
            status="failed",
            request=BackgroundTaskRequestSnapshot(
                prompt="delegate with api_key=task-prompt-secret",
                parent_session_id="bundle-session",
            ),
            session_id="child-secret-session",
            error="failed with Bearer taskerrorsecret",
        ),
    )


def _save_session_with_tool_artifact(tmp_path: Path) -> dict[str, object]:
    store = SqliteSessionStore()
    content = "".join(f"artifact-line-{index}\n" for index in range(8))
    capped = cap_tool_result_output(
        ToolResult(tool_name="shell_exec", status="ok", content=content),
        workspace=tmp_path,
        session_id="artifact-session",
        tool_call_id="artifact-call",
        max_lines=2,
        max_bytes=10_000,
    )
    payload = {
        **capped.data,
        "tool": "shell_exec",
        "tool_call_id": "artifact-call",
        "status": capped.status,
        "content": capped.content,
        "error": capped.error,
    }
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="artifact-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="artifact-session",
                sequence=1,
                event_type="runtime.tool_completed",
                source="tool",
                payload=payload,
            ),
        ),
        output="done",
    )
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="artifact", session_id="artifact-session"),
        response=response,
    )
    return payload


def _minimal_bundle_payload(sessions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "voidcode.session.bundle.v1",
        "manifest": {
            "schema_version": 1,
            "voidcode_version": "0.1.0",
            "created_at": 1,
            "workspace_hash": "sha256:test",
            "platform": {},
            "redaction": {"redacted": True},
            "support_mode": False,
            "session_count": len(sessions),
            "event_count": sum(
                len(cast(list[object], session.get("events", []))) for session in sessions
            ),
            "background_task_count": 0,
        },
        "sessions": sessions,
        "background_tasks": [],
        "diagnostics": {},
    }


def _minimal_session_payload(
    session_id: str,
    *,
    parent_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": session_id,
        "parent_id": parent_id,
        "status": "completed",
        "turn": 1,
        "prompt": f"prompt {session_id}",
        "output": f"output {session_id}",
        "metadata": {},
        "last_event_sequence": 1,
        "events": [
            {
                "sequence": 1,
                "event_type": "graph.response_ready",
                "source": "graph",
                "payload": {"summary": session_id},
            }
        ],
    }


def test_session_bundle_export_redacts_and_bounds_default_payload(tmp_path: Path) -> None:
    _save_sample_session(tmp_path)
    store = SqliteSessionStore()

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="bundle-session",
        options=SessionBundleOptions(tool_output_preview_chars=16),
        storage_diagnostics={"authorization": "Bearer storage-secret"},
        config_summary={"api_key": "sk-config-secret"},
    )
    payload = bundle.to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["schema"] == SESSION_BUNDLE_SCHEMA_NAME
    assert bundle.manifest.session_count == 1
    assert bundle.manifest.event_count == 2
    assert "raw provider content" not in encoded
    assert "sk-test-secret" not in encoded
    assert "prompt-secret" not in encoded
    assert "sk-output-secret" not in encoded
    assert "private chain of thought" not in encoded
    assert "Bearer abcdef123456" not in encoded
    assert SESSION_BUNDLE_REDACTED_PLACEHOLDER in encoded
    assert "truncated by session bundle" in encoded


def test_session_bundle_export_redacts_background_task_prompt_and_error(
    tmp_path: Path,
) -> None:
    _save_sample_session(tmp_path)
    _save_background_task_with_secrets(tmp_path)
    store = SqliteSessionStore()

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="bundle-session",
    )
    payload = bundle.to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert bundle.manifest.background_task_count == 1
    assert "task-prompt-secret" not in encoded
    assert "taskerrorsecret" not in encoded
    background_tasks = cast(list[dict[str, object]], payload["background_tasks"])
    assert background_tasks[0]["prompt"] == "delegate with <redacted>"
    assert background_tasks[0]["error"] == "failed with <redacted>"


def test_session_bundle_json_and_zip_roundtrip(tmp_path: Path) -> None:
    _save_sample_session(tmp_path)
    store = SqliteSessionStore()
    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="bundle-session",
    )

    json_bundle = read_session_bundle_bytes(serialize_session_bundle(bundle, fmt="json"))
    zip_bundle = read_session_bundle_bytes(serialize_session_bundle(bundle, fmt="zip"))

    assert json_bundle.to_payload() == bundle.to_payload()
    assert zip_bundle.to_payload() == bundle.to_payload()


def test_session_bundle_includes_available_artifacts_only_when_tool_output_requested(
    tmp_path: Path,
) -> None:
    tool_payload = _save_session_with_tool_artifact(tmp_path)
    store = SqliteSessionStore()

    default_bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="artifact-session",
    )
    full_bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="artifact-session",
        options=SessionBundleOptions(include_tool_output=True),
    )

    assert default_bundle.manifest.artifact_count == 0
    assert default_bundle.artifacts == ()
    assert full_bundle.manifest.artifact_count == 1
    artifact = full_bundle.artifacts[0]
    assert artifact.artifact_id == tool_payload["artifact_id"]
    assert artifact.tool_call_id == "artifact-call"
    assert artifact.missing is False
    assert artifact.content is not None
    assert artifact.content.endswith("artifact-line-7\n")
    assert full_bundle.to_payload()["artifacts"] == [
        {
            "artifact_id": artifact.artifact_id,
            "session_id": "artifact-session",
            "tool_call_id": "artifact-call",
            "tool_name": "shell_exec",
            "metadata": artifact.metadata,
            "content": artifact.content,
            "missing": False,
        }
    ]


def test_session_bundle_reports_missing_artifact_without_content(tmp_path: Path) -> None:
    tool_payload = _save_session_with_tool_artifact(tmp_path)
    artifact = tool_payload["artifact"]
    assert isinstance(artifact, dict)
    Path(cast(str, artifact["path"])).unlink()
    store = SqliteSessionStore()

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="artifact-session",
        options=SessionBundleOptions(include_tool_output=True),
    )

    assert bundle.manifest.artifact_count == 1
    bundled_artifact = bundle.artifacts[0]
    assert bundled_artifact.artifact_id == tool_payload["artifact_id"]
    assert bundled_artifact.missing is True
    assert bundled_artifact.content is None
    assert bundled_artifact.metadata["status"] == "missing"


def test_session_bundle_import_roundtrip_never_overwrites_existing_session(
    tmp_path: Path,
) -> None:
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    source_workspace.mkdir()
    target_workspace.mkdir()
    _save_sample_session(source_workspace)
    _save_sample_session(target_workspace)
    source_store = SqliteSessionStore()
    target_store = SqliteSessionStore()
    bundle = build_session_bundle(
        session_store=source_store,
        workspace=source_workspace,
        session_id="bundle-session",
    )

    result = apply_session_bundle(
        bundle,
        session_store=target_store,
        workspace=target_workspace,
    )
    loaded = target_store.load_session(
        workspace=target_workspace,
        session_id="bundle-session-imported",
    )

    assert result.imported_session_ids == ("bundle-session-imported",)
    assert loaded.session.metadata["imported_bundle"] == {
        "version": 1,
        "original_session_id": "bundle-session",
        "imported_at_session_id": "bundle-session-imported",
    }
    assert loaded.events[-1].sequence == 3


def test_session_bundle_unknown_schema_fails_fast() -> None:
    with pytest.raises(SessionBundleError, match="unsupported session bundle schema"):
        parse_session_bundle({"schema": "voidcode.session.bundle.v999", "manifest": {}})


def test_session_bundle_parse_rejects_invalid_session_ids() -> None:
    invalid_id_payload = _minimal_bundle_payload([_minimal_session_payload("")])
    invalid_parent_payload = _minimal_bundle_payload(
        [_minimal_session_payload("child", parent_id="bad/parent")]
    )

    with pytest.raises(SessionBundleError, match=r"sessions\[0\]\.id is invalid"):
        parse_session_bundle(invalid_id_payload)
    with pytest.raises(SessionBundleError, match=r"sessions\[0\]\.parent_id is invalid"):
        parse_session_bundle(invalid_parent_payload)


def test_session_bundle_import_remaps_child_parent_after_full_id_resolution(
    tmp_path: Path,
) -> None:
    target_store = SqliteSessionStore()
    _save_sample_session(tmp_path, session_id="parent")
    bundle = parse_session_bundle(
        _minimal_bundle_payload(
            [
                _minimal_session_payload("child", parent_id="parent"),
                _minimal_session_payload("parent"),
            ]
        )
    )

    result = apply_session_bundle(
        bundle,
        session_store=target_store,
        workspace=tmp_path,
    )
    imported_child = target_store.load_session(workspace=tmp_path, session_id="child")
    imported_parent = target_store.load_session(workspace=tmp_path, session_id="parent-imported")

    assert result.imported_session_ids == ("child", "parent-imported")
    assert imported_child.session.session.parent_id == "parent-imported"
    assert imported_parent.session.session.id == "parent-imported"


def test_session_bundle_dry_run_import_does_not_persist(tmp_path: Path) -> None:
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    source_workspace.mkdir()
    target_workspace.mkdir()
    _save_sample_session(source_workspace)
    source_store = SqliteSessionStore()
    target_store = SqliteSessionStore()
    bundle = build_session_bundle(
        session_store=source_store,
        workspace=source_workspace,
        session_id="bundle-session",
    )

    result = apply_session_bundle(
        bundle,
        session_store=target_store,
        workspace=target_workspace,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.imported_session_ids == ("bundle-session",)
    assert not target_store.has_session(workspace=target_workspace, session_id="bundle-session")
