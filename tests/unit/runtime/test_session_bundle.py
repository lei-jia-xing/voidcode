from __future__ import annotations

import json
from pathlib import Path

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


def _save_sample_session(tmp_path: Path, *, session_id: str = "bundle-session") -> None:
    store = SqliteSessionStore()
    request = RuntimeRequest(prompt="inspect failure", session_id=session_id)
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
        output="done",
    )
    store.save_run(workspace=tmp_path, request=request, response=response)


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
    assert "private chain of thought" not in encoded
    assert "Bearer abcdef123456" not in encoded
    assert SESSION_BUNDLE_REDACTED_PLACEHOLDER in encoded
    assert "truncated by session bundle" in encoded


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
