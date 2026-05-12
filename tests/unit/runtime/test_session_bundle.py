from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import voidcode.runtime.bundle as bundle_module
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
from voidcode.runtime.workflow_snapshot import workflow_snapshot_from_metadata
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


def _save_sample_memory_records(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.add_memory(
        workspace=tmp_path,
        content="private deployment memory must stay outside support bundles",
        kind="project",
        tags=("bundle-private", "memory-record"),
        source_session_id="bundle-session",
    )
    store.add_memory(
        workspace=tmp_path,
        content="semantic vector seed must not become bundle payload",
        kind="reference",
        tags=("vector-index-cache",),
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


def test_session_bundle_export_excludes_workspace_memory_records(tmp_path: Path) -> None:
    _save_sample_session(tmp_path)
    _save_sample_memory_records(tmp_path)
    store = SqliteSessionStore()

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="bundle-session",
    )
    payload = bundle.to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert store.list_memories(workspace=tmp_path)
    assert set(payload) == {
        "schema",
        "manifest",
        "sessions",
        "background_tasks",
        "diagnostics",
        "artifacts",
    }
    assert "memories" not in payload
    assert "memory_records" not in payload
    assert "private deployment memory" not in encoded
    assert "semantic vector seed" not in encoded
    assert "bundle-private" not in encoded
    assert "memory-record" not in encoded
    assert "vector-index-cache" not in encoded


def test_session_bundle_export_excludes_vector_index_and_cache_payloads(tmp_path: Path) -> None:
    _save_sample_session(tmp_path)
    _save_sample_memory_records(tmp_path)
    store = SqliteSessionStore()

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="bundle-session",
        options=SessionBundleOptions.support_artifact(),
        storage_diagnostics={
            "database_tables": ["sessions", "memories", "memory_vectors", "vector_cache"],
            "vector_index_path": str(tmp_path / "sqlite-vec" / "private-index.bin"),
            "embedding_cache": {
                "cached_text": "semantic vector seed must not become bundle payload"
            },
        },
        config_summary={
            "memory": {"semantic_search": "auto", "sqlite_vec": {"enabled": "auto"}},
            "memory_index_cache": "private vector cache status",
        },
    )
    payload = bundle.to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    assert bundle.manifest.support_mode is True
    assert payload["artifacts"] == []
    assert "memory_vectors" not in encoded
    assert "vector_cache" not in encoded
    assert "vector_index_path" not in encoded
    assert "embedding_cache" not in encoded
    assert "memory_index_cache" not in encoded
    assert "private-index.bin" not in encoded
    assert "private vector cache status" not in encoded
    assert "semantic vector seed" not in encoded


def test_session_bundle_preserves_unrelated_deferred_word_diagnostics(
    tmp_path: Path,
) -> None:
    _save_sample_session(tmp_path)
    store = SqliteSessionStore()

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="bundle-session",
        options=SessionBundleOptions.support_artifact(),
        storage_diagnostics={
            "database_tables": ["sessions", "session_memory_notes"],
            "ordinary_cache_note": "provider cache was warm during diagnostics",
            "search_index_notice": "plain index counter is not bundle payload",
        },
        config_summary={
            "memory": {
                "recall_note": "memory recall remains disabled for this session",
                "vector_mode_note": "vector wording in prose is not vector data",
            },
        },
        provider_summary={
            "cache_status_text": "cache mention without deferred cache payload",
        },
    )
    payload = bundle.to_payload()
    encoded = json.dumps(payload, sort_keys=True)

    diagnostics = cast(dict[str, object], payload["diagnostics"])
    storage = cast(dict[str, object], diagnostics["storage"])
    config = cast(dict[str, object], diagnostics["config_summary"])
    provider = cast(dict[str, object], diagnostics["provider_summary"])
    assert storage["database_tables"] == ["sessions", "session_memory_notes"]
    assert "provider cache was warm during diagnostics" in encoded
    assert "plain index counter is not bundle payload" in encoded
    assert cast(dict[str, object], config["memory"])["recall_note"] == (
        "memory recall remains disabled for this session"
    )
    assert provider["cache_status_text"] == "cache mention without deferred cache payload"


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


def test_session_bundle_roundtrips_snapshot_first_workflow_metadata(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    metadata: dict[str, object] = {
        "workflow_preset": "frontend",
        "workflow": {
            "snapshot_version": 2,
            "requested": {"workflow_mode": "product", "workflow_preset": "frontend"},
            "effective": {
                "mode": "product",
                "legacy_preset": "frontend",
                "source": "workflow_preset",
                "category": "frontend",
                "default_agent": "leader",
                "effective_agent": "leader",
                "read_only_default": False,
                "prompt_append": "Stored frontend guidance.",
                "hook_preset_refs": ["role_reminder"],
                "skill_refs": ["frontend-design", "playwright"],
                "force_load_skills": [],
                "mcp_binding_intents": [{"servers": ["playwright"], "required": False}],
                "verification_guidance": "Run stored frontend checks.",
            },
        },
    }
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="bundle workflow", session_id="workflow-bundle-session"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="workflow-bundle-session"),
                status="completed",
                turn=1,
                metadata=metadata,
            ),
            events=(
                EventEnvelope(
                    session_id="workflow-bundle-session",
                    sequence=1,
                    event_type="graph.response_ready",
                    source="graph",
                ),
            ),
            output="done",
        ),
    )

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="workflow-bundle-session",
    )
    parsed = parse_session_bundle(bundle.to_payload())
    target_workspace = tmp_path / "imported"
    target_workspace.mkdir()
    apply_session_bundle(parsed, session_store=store, workspace=target_workspace)

    bundle_payload = bundle.to_payload()
    bundle_sessions = cast(list[object], bundle_payload["sessions"])
    bundled_session = cast(dict[str, object], bundle_sessions[0])
    bundled_metadata = bundled_session["metadata"]
    imported = store.load_session(workspace=target_workspace, session_id="workflow-bundle-session")
    bundled_snapshot = workflow_snapshot_from_metadata(cast(dict[str, object], bundled_metadata))
    imported_snapshot = workflow_snapshot_from_metadata(imported.session.metadata)

    assert bundled_snapshot == workflow_snapshot_from_metadata(metadata)
    assert imported_snapshot == workflow_snapshot_from_metadata(metadata)


def test_session_bundle_import_preserves_legacy_workflow_preset_only_metadata(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    bundle = parse_session_bundle(
        _minimal_bundle_payload(
            [
                {
                    **_minimal_session_payload("legacy-workflow-bundle"),
                    "metadata": {"workflow_preset": "research"},
                }
            ]
        )
    )
    target_workspace = tmp_path / "imported-legacy"
    target_workspace.mkdir()

    apply_session_bundle(bundle, session_store=store, workspace=target_workspace)

    imported = store.load_session(
        workspace=target_workspace,
        session_id="legacy-workflow-bundle",
    )
    snapshot = workflow_snapshot_from_metadata(imported.session.metadata)

    assert imported.session.metadata["workflow_preset"] == "research"
    assert snapshot is not None
    assert snapshot["requested"] == {"workflow_mode": None, "workflow_preset": "research"}
    assert snapshot["effective"] == {
        "mode": None,
        "legacy_preset": "research",
        "source": None,
    }
    assert snapshot["selected_preset"] == "research"


def test_session_bundle_import_preserves_requested_and_effective_workflow_mode(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    metadata: dict[str, object] = {
        "workflow_mode": "review",
        "workflow": {
            "snapshot_version": 2,
            "requested": {"workflow_mode": "review", "workflow_preset": None},
            "effective": {
                "mode": "review",
                "legacy_preset": None,
                "source": "workflow_mode",
                "category": "review",
                "default_agent": "leader",
                "effective_agent": "leader",
                "read_only_default": False,
                "prompt_append": "Stored review guidance.",
                "hook_preset_refs": [],
                "skill_refs": ["review-work"],
                "force_load_skills": [],
                "mcp_binding_intents": [],
                "verification_guidance": "Use stored review checks.",
            },
        },
    }
    bundle = parse_session_bundle(
        _minimal_bundle_payload(
            [
                {
                    **_minimal_session_payload("new-workflow-bundle"),
                    "metadata": metadata,
                }
            ]
        )
    )
    target_workspace = tmp_path / "imported-new"
    target_workspace.mkdir()

    apply_session_bundle(bundle, session_store=store, workspace=target_workspace)

    imported = store.load_session(workspace=target_workspace, session_id="new-workflow-bundle")

    imported_workflow = workflow_snapshot_from_metadata(imported.session.metadata)
    expected_workflow = workflow_snapshot_from_metadata(metadata)

    assert imported_workflow == expected_workflow
    assert imported.session.metadata.get("mode", "normal") == "normal"
    assert imported.session.metadata.get("mode") != "review"


def test_session_bundle_export_import_normalizes_legacy_workflow_mode_pollution(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    polluted_metadata: dict[str, object] = {
        "mode": "deep_work",
        "read_only": True,
        "workflow": {
            "snapshot_version": 2,
            "requested": {"workflow_mode": "deep_work", "workflow_preset": "research"},
            "effective": {
                "mode": "deep_work",
                "legacy_preset": "research",
                "source": "workflow_mode",
                "read_only_default": True,
            },
        },
        "runtime_policy": {"version": 1, "mode": "deep_work", "read_only": True},
    }
    bundle = parse_session_bundle(
        _minimal_bundle_payload(
            [
                {
                    **_minimal_session_payload("polluted-workflow-bundle"),
                    "metadata": polluted_metadata,
                }
            ]
        )
    )
    target_workspace = tmp_path / "imported-polluted"
    target_workspace.mkdir()

    apply_session_bundle(bundle, session_store=store, workspace=target_workspace)
    exported = build_session_bundle(
        session_store=store,
        workspace=target_workspace,
        session_id="polluted-workflow-bundle",
    )

    imported = store.load_session(
        workspace=target_workspace,
        session_id="polluted-workflow-bundle",
    )
    exported_metadata = exported.sessions[0].metadata
    workflow = cast(dict[str, object], imported.session.metadata["workflow"])

    assert imported.session.metadata["mode"] == "normal"
    assert imported.session.metadata["read_only"] is True
    assert cast(dict[str, object], imported.session.metadata["runtime_policy"])["mode"] == "normal"
    assert exported_metadata["mode"] == "normal"
    assert cast(dict[str, object], exported_metadata["runtime_policy"])["mode"] == "normal"
    assert cast(dict[str, object], workflow["effective"])["mode"] == "deep_work"


def test_session_bundle_export_import_preserves_redacted_runtime_policy_metadata(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    metadata: dict[str, object] = {
        "mode": "plan",
        "read_only": False,
        "prompt_stack": {
            "version": 1,
            "fragments": [{"source": "user", "preview": "Bearer rawpromptsecret"}],
        },
        "runtime_state": {"injected_env": {"NPM_CONFIG_YES": "true"}},
    }
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(
            prompt="bundle policy token=promptsecret",
            session_id="policy-bundle",
        ),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="policy-bundle"),
                status="failed",
                turn=1,
                metadata=metadata,
            ),
            events=(
                EventEnvelope(
                    session_id="policy-bundle",
                    sequence=1,
                    event_type="runtime.failed",
                    source="runtime",
                    payload={
                        "kind": "runtime_tool_policy_denied",
                        "tool": "write_file",
                        "tool_policy": {
                            "tool": "write_file",
                            "mode": "plan",
                            "read_only": True,
                            "decision": "deny",
                        },
                    },
                ),
            ),
        ),
    )

    built = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="policy-bundle",
    )
    parsed = parse_session_bundle(built.to_payload())
    target_workspace = tmp_path / "imported-policy"
    target_workspace.mkdir()
    apply_session_bundle(parsed, session_store=store, workspace=target_workspace)

    encoded = json.dumps(built.to_payload(), sort_keys=True)
    bundled_metadata = built.sessions[0].metadata
    imported = store.load_session(workspace=target_workspace, session_id="policy-bundle")

    assert bundled_metadata["mode"] == "plan"
    assert bundled_metadata["read_only"] is True
    assert imported.session.metadata["mode"] == "plan"
    assert imported.session.metadata["read_only"] is True
    assert imported.session.metadata["runtime_policy"] == bundled_metadata["runtime_policy"]
    assert "rawpromptsecret" not in encoded
    assert "promptsecret" not in encoded
    assert 'NPM_CONFIG_YES": "true' not in encoded


def test_session_bundle_preserves_prompt_activation_records_without_raw_guidance(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    activation_record: dict[str, object] = {
        "key": "agent_prompt:leader|mode:plan|intent:unspecified",
        "activation_id": "agent_prompt:leader",
        "mode": "plan",
        "intent_slot": "unspecified",
        "source": "runtime_policy",
        "guidance_only": True,
        "raw_prompt_stored": False,
        "preview": "Activation: agent_prompt:leader token=activation-secret",
        "preview_truncated": False,
    }
    metadata: dict[str, object] = {
        "mode": "plan",
        "runtime_policy": {
            "schema_version": 1,
            "policy_version": "v1",
            "agent_preset": "leader",
            "agent_manifest_id": "leader",
            "intent": {"label": "unspecified"},
            "tool_policy": {},
            "delegation_policy": {},
            "hook_policy": {},
            "prompt_activation": {
                "enabled": True,
                "activated_this_turn": True,
                "activated": [activation_record],
                "last_activation": activation_record,
                "raw_prompt_stored": False,
            },
            "precedence_trace": [],
            "diagnostics": {},
            "mode": "plan",
            "read_only": True,
        },
    }
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="activation bundle", session_id="activation-bundle"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="activation-bundle"),
                status="completed",
                turn=1,
                metadata=metadata,
            ),
            events=(
                EventEnvelope(
                    session_id="activation-bundle",
                    sequence=1,
                    event_type="graph.response_ready",
                    source="graph",
                    payload={"summary": "done"},
                ),
            ),
        ),
    )

    built = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="activation-bundle",
    )
    target_workspace = tmp_path / "imported-activation"
    target_workspace.mkdir()
    apply_session_bundle(
        parse_session_bundle(built.to_payload()),
        session_store=store,
        workspace=target_workspace,
    )

    encoded = json.dumps(built.to_payload(), sort_keys=True)
    bundled_policy = cast(dict[str, object], built.sessions[0].metadata["runtime_policy"])
    bundled_activation = cast(dict[str, object], bundled_policy["prompt_activation"])
    imported = store.load_session(workspace=target_workspace, session_id="activation-bundle")

    assert bundled_activation["activated_this_turn"] is True
    assert cast(list[object], bundled_activation["activated"])
    assert "activation-secret" not in encoded
    assert "<prompt_activation_guidance>" not in encoded
    assert imported.session.metadata["runtime_policy"] == bundled_policy


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
            "content_truncated": False,
            "content_next_offset": None,
        }
    ]


def test_session_bundle_marks_artifact_content_truncated_when_read_limit_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_payload = _save_session_with_tool_artifact(tmp_path)
    store = SqliteSessionStore()
    monkeypatch.setattr(bundle_module, "_BUNDLE_ARTIFACT_READ_LIMIT_LINES", 2)

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="artifact-session",
        options=SessionBundleOptions(include_tool_output=True),
    )

    assert bundle.manifest.artifact_count == 1
    artifact = bundle.artifacts[0]
    assert artifact.artifact_id == tool_payload["artifact_id"]
    assert artifact.missing is False
    assert artifact.content == "artifact-line-0\nartifact-line-1\n"
    assert artifact.content_truncated is True
    assert artifact.content_next_offset == 2
    assert artifact.metadata["content_truncated"] is True
    assert artifact.metadata["content_next_offset"] == 2
    assert artifact.metadata["bundle_read_limit_lines"] == 2


def test_session_bundle_reports_missing_artifact_without_content(tmp_path: Path) -> None:
    tool_payload = _save_session_with_tool_artifact(tmp_path)
    artifact = tool_payload["artifact"]
    assert isinstance(artifact, dict)
    artifact_payload = cast(dict[str, object], artifact)
    Path(cast(str, artifact_payload["path"])).unlink()
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


def test_session_bundle_skips_forged_artifact_paths(tmp_path: Path) -> None:
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("must not be bundled", encoding="utf-8")
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="forged-artifact-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="forged-artifact-session",
                sequence=1,
                event_type="runtime.tool_completed",
                source="tool",
                payload={
                    "tool": "shell_exec",
                    "tool_call_id": "forged-call",
                    "status": "ok",
                    "content": "forged",
                    "artifact": {
                        "producer": "voidcode.tool_output.v1",
                        "artifact_id": "artifact_forged",
                        "tool_call_id": "forged-call",
                        "path": str(secret_path),
                        "status": "available",
                    },
                },
            ),
        ),
        output="done",
    )
    store = SqliteSessionStore()
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="forged", session_id="forged-artifact-session"),
        response=response,
    )

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="forged-artifact-session",
        options=SessionBundleOptions(include_tool_output=True),
    )
    encoded = json.dumps(bundle.to_payload(), sort_keys=True)

    assert bundle.manifest.artifact_count == 0
    assert bundle.artifacts == ()
    assert "must not be bundled" not in encoded


def test_session_bundle_skips_short_id_forged_temp_artifact_path(tmp_path: Path) -> None:
    real_payload = _save_session_with_tool_artifact(tmp_path)
    real_artifact = real_payload["artifact"]
    assert isinstance(real_artifact, dict)
    forged_artifact = {
        **cast(dict[str, object], real_artifact),
        "artifact_id": "artifact_",
        "tool_call_id": "forged-call",
    }
    response = RuntimeResponse(
        session=SessionState(
            session=SessionRef(id="forged-temp-artifact-session"),
            status="completed",
            turn=1,
            metadata={},
        ),
        events=(
            EventEnvelope(
                session_id="forged-temp-artifact-session",
                sequence=1,
                event_type="runtime.tool_completed",
                source="tool",
                payload={
                    "tool": "shell_exec",
                    "tool_call_id": "forged-call",
                    "status": "ok",
                    "content": "forged",
                    "artifact": forged_artifact,
                },
            ),
        ),
        output="done",
    )
    store = SqliteSessionStore()
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="forged", session_id="forged-temp-artifact-session"),
        response=response,
    )

    bundle = build_session_bundle(
        session_store=store,
        workspace=tmp_path,
        session_id="forged-temp-artifact-session",
        options=SessionBundleOptions(include_tool_output=True),
    )
    encoded = json.dumps(bundle.to_payload(), sort_keys=True)

    assert bundle.manifest.artifact_count == 0
    assert bundle.artifacts == ()
    assert "artifact-line-7" not in encoded


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


def test_session_bundle_import_rejects_unsupported_runtime_policy_snapshot_version(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    bundle = parse_session_bundle(
        _minimal_bundle_payload(
            [
                {
                    **_minimal_session_payload("future-policy-bundle"),
                    "metadata": {
                        "runtime_policy": {
                            "schema_version": 999,
                            "policy_version": "v1",
                        }
                    },
                }
            ]
        )
    )

    with pytest.raises(ValueError, match="unsupported runtime_policy schema_version"):
        apply_session_bundle(bundle, session_store=store, workspace=tmp_path)

    assert not store.has_session(workspace=tmp_path, session_id="future-policy-bundle")


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
