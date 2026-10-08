from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from tests.runtime_composition import save_checkpoint
from voidcode.runtime.composition import CompositionRef, PhaseRecord
from voidcode.runtime.storage import SqliteSessionStore


def test_private_phase_state_survives_stale_snapshots_and_position_changes(tmp_path: Path) -> None:
    owner = SqliteSessionStore(database_path=tmp_path / "phases.sqlite3")
    save_checkpoint(owner, workspace=tmp_path, session_id="owner")
    stale = owner.load_session(workspace=tmp_path, session_id="owner").session.metadata
    turn = {
        "session_id": "owner",
        "run_id": "producer",
        "scope_id": "owner:producer:1",
        "occurrence": 1,
        "completed": False,
        "invoked_call_ids": ["actual-call"],
    }
    current = {**stale, "runtime_state": {"composition_turn_occurrence": 1, "composition_turn": turn}}
    owner.update_session_metadata(workspace=tmp_path, session_id="owner", metadata=current)
    record = PhaseRecord.model_validate(
        {
            "phase_key": "package:phase",
            "binding_id": CompositionRef.model_validate(stale["composition_ref"]).binding_id,
            "version": "1",
            "scope": "turn",
            "scope_id": turn["scope_id"],
            "failed": False,
            "state": "completed",
            "payload": {"text": "exact payload " * 2000, "items": list(range(100))},
        }
    )
    owner.save_composition_phase_record(workspace=tmp_path, session_id="owner", record=record)
    owner.update_session_metadata(
        workspace=tmp_path,
        session_id="owner",
        metadata={
            **current,
            "runtime_state": {"composition_turn_occurrence": 1, "composition_turn": {**turn, "completed": True}},
        },
    )
    owner.update_session_metadata(workspace=tmp_path, session_id="owner", metadata=current)
    save_checkpoint(owner, workspace=tmp_path, session_id="owner", session_metadata=stale)
    checkpoint = owner.load_resume_checkpoint(workspace=tmp_path, session_id="owner")
    assert checkpoint is not None
    durable = cast(dict[str, Any], owner.load_session(workspace=tmp_path, session_id="owner").session.metadata)
    key = f"{record.phase_key}:{record.scope}:{record.scope_id}"
    assert durable["phase_records"][key] == record.model_dump(mode="json")
    assert durable["runtime_state"]["composition_turn"]["completed"] is True
    checkpoint_metadata = cast(dict[str, Any], checkpoint["session_metadata"])
    assert checkpoint_metadata["phase_records"] == durable["phase_records"]
    assert checkpoint_metadata["runtime_state"] == durable["runtime_state"]
    events = owner.append_session_events(
        workspace=tmp_path,
        session_id="owner",
        events=(("runtime.request_received", "runtime", {"prompt": "continue"}, None),),
        interrupted_checkpoint=checkpoint,
    )
    child = owner.fork_session(workspace=tmp_path, session_id="owner")
    child_metadata = owner.load_session(workspace=tmp_path, session_id=child.session.id).session.metadata
    assert "phase_records" not in child_metadata and "runtime_state" not in child_metadata
    owner.checkout_session(workspace=tmp_path, session_id="owner", sequence=events[0].sequence)
    checked_out = owner.load_session(workspace=tmp_path, session_id="owner").session.metadata
    assert checked_out["phase_records"] == durable["phase_records"]
    assert checked_out["runtime_state"] == {"composition_turn_occurrence": 1}
    owner.update_session_metadata(workspace=tmp_path, session_id="owner", metadata=current)
    assert owner.load_session(workspace=tmp_path, session_id="owner").session.metadata["runtime_state"] == {"composition_turn_occurrence": 1}
    next_turn = {**turn, "scope_id": "owner:producer:2", "occurrence": 2}
    owner.update_session_metadata(
        workspace=tmp_path,
        session_id="owner",
        metadata={
            **checked_out,
            "runtime_state": {"composition_turn_occurrence": 2, "composition_turn": next_turn},
        },
    )
    owner.update_session_metadata(workspace=tmp_path, session_id="owner", metadata=current)
    save_checkpoint(owner, workspace=tmp_path, session_id="owner", session_metadata=stale)
    advanced = cast(dict[str, Any], owner.load_session(workspace=tmp_path, session_id="owner").session.metadata)
    assert advanced["runtime_state"]["composition_turn_occurrence"] == 2
    assert advanced["runtime_state"]["composition_turn"] == next_turn


@pytest.mark.parametrize(
    "corruption",
    [
        {"phase_records": None},
        {"phase_records": {"bad": None}},
        {"runtime_state": None},
        {"runtime_state": {"composition_turn_occurrence": True}},
        {"runtime_state": {"composition_turn_occurrence": 1, "composition_turn": None}},
    ],
)
def test_stale_checkpoint_cannot_erase_invalid_private_state(tmp_path: Path, corruption: dict[str, object]) -> None:
    database = tmp_path / "phases.sqlite3"
    owner = SqliteSessionStore(database_path=database)
    save_checkpoint(owner, workspace=tmp_path, session_id="owner")
    stale = owner.load_session(workspace=tmp_path, session_id="owner").session.metadata
    corrupted = {**stale, **corruption}
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET metadata_json = ? WHERE session_id = ?", (json.dumps(corrupted), "owner"))
    # Invalid entries may fail closed at the storage boundary or remain untouched
    # for canonical lifecycle admission; neither path may erase/filter corruption.
    try:
        save_checkpoint(owner, workspace=tmp_path, session_id="owner", session_metadata=stale)
    except ValueError:
        pass
    with sqlite3.connect(database) as connection:
        raw = connection.execute("SELECT metadata_json FROM sessions WHERE session_id = ?", ("owner",)).fetchone()
    assert raw is not None
    assert json.loads(raw[0]) == corrupted
