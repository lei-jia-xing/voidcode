"""Phase 1 delegation flexibility: invocation-level outputSchema + schemaMode.

Covers the finalize-time schema validation (permissive/strict), storage v12
round-trip + migration, the no-schema and keep-alive intermediate-turn guards,
and the CLI/HTTP result surfaces. The completion evidence chain itself
(``child_terminal.py``) is untouched; these tests only assert the schema layer
added around it.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from voidcode.runtime import VoidCodeRuntime
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import BackgroundTaskResult
from voidcode.runtime.http import RuntimeTransportApp
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    SchemaValidation,
)

DECLARED_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


def _delegation(schema_mode: str = "permissive") -> dict[str, object]:
    return {
        "mode": "background",
        "subagent_type": "worker",
        "output_schema": dict(DECLARED_SCHEMA),
        "schema_mode": schema_mode,
    }


def _runtime_with_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[VoidCodeRuntime, SqliteSessionStore]:
    store = SqliteSessionStore()
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(db_path))
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=store,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False)),
    )
    return runtime, store


def _seed_interrupted_child_with_handoff(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    task_id: str,
    child_session_id: str,
    parent_session_id: str = "leader-session",
    data: dict[str, object] | None = None,
    delegation: dict[str, object] | None = None,
) -> None:
    """Seed a task + child whose ROW is ``interrupted`` but whose transcript
    proves a successful ``submit_result`` handoff — the exact unsealed-seal
    state the run loop can leave behind and the shape a keep-alive final turn
    takes before finalize upgrades it."""
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=child_session_id,
        prompt="child probe",
        session_metadata={
            "background_run": True,
            "background_task_id": task_id,
        },
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    handoff: dict[str, object] = {"summary": "done"}
    arguments: dict[str, object] = {"summary": "done"}
    if data is not None:
        handoff["data"] = data
        arguments["data"] = data
    store.append_session_events(
        workspace=workspace,
        session_id=child_session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "child probe"}, None),
            (
                "runtime.tool_completed",
                "tool",
                {
                    "tool": "submit_result",
                    "status": "ok",
                    "arguments": arguments,
                    "handoff": handoff,
                },
                None,
            ),
            ("graph.response_ready", "graph", {"output_preview": "done", "source": "submit_result"}, None),
        ),
    )
    metadata: dict[str, object] = {}
    if delegation is not None:
        metadata["delegation"] = delegation
    store.create_background_task(
        workspace=workspace,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="interrupted",
            request=BackgroundTaskRequestSnapshot(
                prompt="child probe",
                parent_session_id=parent_session_id,
                metadata=metadata,
            ),
            session_id=child_session_id,
            error="background task worker exited before a terminal update",
        ),
    )


def _finalize(runtime: VoidCodeRuntime, store: SqliteSessionStore, *, workspace: Path, task_id: str) -> None:
    supervisor = runtime._background_task_supervisor
    task = store.load_background_task(workspace=workspace, task_id=task_id)
    child_response = supervisor.load_background_task_child_response(task=task)
    assert child_response is not None
    supervisor.finalize_background_task_from_session_response(session_response=child_response)


# ── finalize-time validation ────────────────────────────────────────────────


def test_permissive_schema_failure_keeps_task_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime_with_store(tmp_path, monkeypatch)
    task_id = "task-permissive"
    child_session_id = "child-permissive"
    _seed_interrupted_child_with_handoff(
        store,
        workspace=tmp_path,
        task_id=task_id,
        child_session_id=child_session_id,
        data={"answer": 42},
        delegation=_delegation("permissive"),
    )

    _finalize(runtime, store, workspace=tmp_path, task_id=task_id)

    terminal = store.load_background_task(workspace=tmp_path, task_id=task_id)
    assert terminal.status == "completed"
    assert terminal.schema_validation is not None
    assert terminal.schema_validation.valid is False
    assert terminal.schema_validation.schema_mode == "permissive"
    assert terminal.schema_validation.schema_source == "invocation"
    assert "answer" in (terminal.schema_validation.error or "")
    assert terminal.structured_output is None
    # The child row is still sealed by transcript evidence, never rolled back.
    assert store.load_session_status(workspace=tmp_path, session_id=child_session_id) == "completed"


def test_strict_schema_failure_fails_task_and_seals_child_by_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime_with_store(tmp_path, monkeypatch)
    task_id = "task-strict"
    child_session_id = "child-strict"
    _seed_interrupted_child_with_handoff(
        store,
        workspace=tmp_path,
        task_id=task_id,
        child_session_id=child_session_id,
        data={"answer": 42},
        delegation=_delegation("strict"),
    )

    _finalize(runtime, store, workspace=tmp_path, task_id=task_id)

    terminal = store.load_background_task(workspace=tmp_path, task_id=task_id)
    assert terminal.status == "failed"
    assert terminal.error is not None
    assert "schema validation failed" in terminal.error
    assert "answer" in terminal.error
    assert terminal.schema_validation is not None
    assert terminal.schema_validation.valid is False
    assert terminal.schema_validation.schema_mode == "strict"
    assert terminal.structured_output is None
    # The child row is sealed by transcript evidence (completed), not by the
    # task-level strict failure — two layers stay separate.
    assert store.load_session_status(workspace=tmp_path, session_id=child_session_id) == "completed"


def test_valid_structured_output_is_persisted_and_surfaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime_with_store(tmp_path, monkeypatch)
    task_id = "task-valid"
    child_session_id = "child-valid"
    data: dict[str, object] = {"answer": "42"}
    _seed_interrupted_child_with_handoff(
        store,
        workspace=tmp_path,
        task_id=task_id,
        child_session_id=child_session_id,
        data=data,
        delegation=_delegation("permissive"),
    )

    _finalize(runtime, store, workspace=tmp_path, task_id=task_id)

    terminal = store.load_background_task(workspace=tmp_path, task_id=task_id)
    assert terminal.status == "completed"
    assert terminal.structured_output == data
    assert terminal.schema_validation is not None
    assert terminal.schema_validation.valid is True
    assert terminal.schema_validation.error is None

    result = runtime.load_background_task_result(task_id)
    assert result.structured_output == data
    assert result.schema_validation is not None
    assert result.schema_validation.valid is True
    assert result.schema_validation.schema_mode == "permissive"
    assert result.summary_output is not None
    assert "done" in result.summary_output


def test_missing_data_is_validated_as_empty_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime_with_store(tmp_path, monkeypatch)
    task_id = "task-no-data"
    _seed_interrupted_child_with_handoff(
        store,
        workspace=tmp_path,
        task_id=task_id,
        child_session_id="child-no-data",
        data=None,
        delegation=_delegation("permissive"),
    )

    _finalize(runtime, store, workspace=tmp_path, task_id=task_id)

    terminal = store.load_background_task(workspace=tmp_path, task_id=task_id)
    assert terminal.status == "completed"
    assert terminal.schema_validation is not None
    assert terminal.schema_validation.valid is False
    assert terminal.structured_output is None


def test_no_schema_skips_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime_with_store(tmp_path, monkeypatch)
    task_id = "task-plain"
    _seed_interrupted_child_with_handoff(
        store,
        workspace=tmp_path,
        task_id=task_id,
        child_session_id="child-plain",
        data={"anything": "goes"},
        delegation=None,
    )

    _finalize(runtime, store, workspace=tmp_path, task_id=task_id)

    terminal = store.load_background_task(workspace=tmp_path, task_id=task_id)
    assert terminal.status == "completed"
    assert terminal.schema_validation is None
    assert terminal.structured_output is None


def test_keep_alive_intermediate_turn_without_handoff_is_not_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store = _runtime_with_store(tmp_path, monkeypatch)
    task_id = "task-intermediate"
    child_session_id = "child-intermediate"
    # Mid-flight transcript: a tool ran but no submit_result handoff and no
    # graph.response_ready — the turn is genuinely resumable (keep-alive).
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=child_session_id,
        prompt="child probe",
        session_metadata={
            "background_run": True,
            "background_task_id": task_id,
        },
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id=child_session_id,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "child probe"}, None),
            ("runtime.tool_completed", "tool", {"tool": "read", "status": "ok", "content": "probe"}, None),
        ),
    )
    store.create_background_task(
        workspace=tmp_path,
        task=BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="interrupted",
            request=BackgroundTaskRequestSnapshot(
                prompt="child probe",
                parent_session_id="leader-session",
                metadata={"delegation": _delegation("strict")},
            ),
            session_id=child_session_id,
            error="background task worker exited before a terminal update",
        ),
    )

    supervisor = runtime._background_task_supervisor
    task = store.load_background_task(workspace=tmp_path, task_id=task_id)
    child_response = supervisor.load_background_task_child_response(task=task)
    assert child_response is not None
    supervisor.finalize_background_task_from_session_response(session_response=child_response)

    terminal = store.load_background_task(workspace=tmp_path, task_id=task_id)
    # Genuinely resumable: never terminalized, never validated, no schema columns.
    assert terminal.status == "interrupted"
    assert terminal.schema_validation is None
    assert terminal.structured_output is None
    assert store.load_session_status(workspace=tmp_path, session_id=child_session_id) == "interrupted"


# ── storage v12 round-trip + migration ──────────────────────────────────────


def _task_with_delegation(
    *,
    task_id: str,
    delegation: dict[str, object] | None,
) -> BackgroundTaskState:
    metadata: dict[str, object] = {}
    if delegation is not None:
        metadata["delegation"] = delegation
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        request=BackgroundTaskRequestSnapshot(prompt="read sample.txt", metadata=metadata),
        created_at=1,
        updated_at=1,
    )


def test_storage_round_trips_schema_declaration_and_validation(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "schema.db")
    store.create_background_task(
        workspace=tmp_path,
        task=_task_with_delegation(task_id="task-schema", delegation=_delegation("strict")),
    )

    loaded = store.load_background_task(workspace=tmp_path, task_id="task-schema")
    assert loaded.output_schema == DECLARED_SCHEMA
    assert loaded.schema_mode == "strict"
    assert loaded.structured_output is None
    assert loaded.schema_validation is None

    listed = store.list_background_tasks(workspace=tmp_path)
    assert len(listed) == 1
    assert listed[0].output_schema == DECLARED_SCHEMA
    assert listed[0].schema_mode == "strict"

    store.persist_background_task_schema_validation(
        workspace=tmp_path,
        task_id="task-schema",
        structured_output_json=json.dumps({"answer": "42"}, sort_keys=True),
        schema_validation_json=json.dumps(
            {"schema_source": "invocation", "schema_mode": "strict", "valid": True, "error": None},
            sort_keys=True,
        ),
    )

    loaded = store.load_background_task(workspace=tmp_path, task_id="task-schema")
    assert loaded.structured_output == {"answer": "42"}
    assert loaded.schema_validation is not None
    assert loaded.schema_validation.valid is True
    assert loaded.schema_validation.schema_mode == "strict"
    assert loaded.schema_validation.schema_source == "invocation"


def test_storage_fresh_database_is_v12_with_output_schema_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    # Any store operation bootstraps the canonical schema.
    assert store.list_background_tasks(workspace=tmp_path) == ()

    with closing(sqlite3.connect(database_path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(background_tasks)").fetchall()}
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert schema_version == 12
    assert {"output_schema_json", "schema_mode", "structured_output_json", "schema_validation_json"} <= columns


def test_storage_migrates_v11_database_with_output_schema_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "v11.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    store.create_background_task(workspace=tmp_path, task=_task_with_delegation(task_id="task-legacy", delegation=None))

    # Rewind the freshly-bootstrapped (v12) database to the previous released
    # schema (v11): drop the output-schema columns and stamp user_version = 11.
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN output_schema_json")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN schema_mode")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN structured_output_json")
        _ = connection.execute("ALTER TABLE background_tasks DROP COLUMN schema_validation_json")
        _ = connection.execute("PRAGMA user_version = 11")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)
    # Any store operation bootstraps the schema and runs the v11 → v12 migration.
    legacy = store.load_background_task(workspace=tmp_path, task_id="task-legacy")
    listed = store.list_background_tasks(workspace=tmp_path)
    with closing(sqlite3.connect(database_path)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(background_tasks)").fetchall()]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute(
            "SELECT task_id, output_schema_json, schema_mode, structured_output_json, schema_validation_json FROM background_tasks"
        ).fetchall()

    assert schema_version == 12
    for column in ("output_schema_json", "schema_mode", "structured_output_json", "schema_validation_json"):
        assert column in columns
    # Pre-existing v11 rows are preserved and default to no schema.
    assert rows == [("task-legacy", None, "permissive", None, None)]

    assert legacy.output_schema is None
    assert legacy.schema_mode == "permissive"
    assert legacy.structured_output is None
    assert legacy.schema_validation is None
    assert len(listed) == 1
    assert listed[0].output_schema is None
    assert listed[0].schema_mode == "permissive"


# ── CLI / HTTP result surfaces ──────────────────────────────────────────────


def test_cli_tasks_output_json_payload_carries_schema_fields(tmp_path: Path) -> None:
    cli = importlib.import_module("voidcode.cli.app")
    result = BackgroundTaskResult(
        task_id="task-schema-cli",
        parent_session_id="leader-session",
        child_session_id="child-session",
        status="completed",
        summary_output="done",
        result_available=True,
        structured_output={"answer": "42"},
        schema_validation=SchemaValidation(
            schema_source="invocation",
            schema_mode="strict",
            valid=True,
            error=None,
        ),
    )

    payload = cli._background_task_result_payload(result, workspace=tmp_path)

    assert payload["structured_output"] == {"answer": "42"}
    assert payload["schema_validation"] == {
        "schema_source": "invocation",
        "schema_mode": "strict",
        "valid": True,
        "error": None,
    }


def test_http_task_result_serialization_carries_schema_fields() -> None:
    result = BackgroundTaskResult(
        task_id="task-schema-http",
        parent_session_id="leader-session",
        child_session_id="child-session",
        status="completed",
        summary_output="done",
        result_available=True,
        structured_output={"answer": "42"},
        schema_validation=SchemaValidation(
            schema_source="invocation",
            schema_mode="permissive",
            valid=False,
            error="answer: 42 is not of type 'string' (received int)",
        ),
    )

    payload = RuntimeTransportApp._serialize_background_task_result(result)

    assert payload["structured_output"] == {"answer": "42"}
    assert payload["schema_validation"] == {
        "schema_source": "invocation",
        "schema_mode": "permissive",
        "valid": False,
        "error": "answer: 42 is not of type 'string' (received int)",
    }
