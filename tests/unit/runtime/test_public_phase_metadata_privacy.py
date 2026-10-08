from copy import deepcopy

from voidcode.cli_support import serialize_session_state
from voidcode.runtime.contracts import RuntimeResponse, RuntimeSessionDebugSnapshot, RuntimeStreamChunk
from voidcode.runtime.serialization import serialize_session_debug_snapshot
from voidcode.runtime.session import SessionRef, SessionState, session_metadata_for_persistence
from voidcode.runtime.transport.http import RuntimeTransportApp, _SessionStateEmitter


def test_phase_metadata_stays_durable_but_never_reaches_public_consumers() -> None:
    metadata: dict[str, object] = {
        "phase_records": {
            f"phase:turn:turn-{index}": {
                "phase_key": "phase",
                "binding_id": "binding",
                "version": "1",
                "scope": "turn",
                "scope_id": f"turn-{index}",
                "failed": False,
                "state": "completed",
                "payload": {
                    "snapshot": {
                        "content": "private-phase-body" * 200,
                        "secret": "private phase state",
                        "rows": [{"value": item} for item in range(60)],
                        "mapping": {f"field-{item}": item for item in range(110)},
                    }
                },
            }
            for index in range(110)
        },
        "composition_turn_occurrence": 7,
        "runtime_state": {
            "composition_turn_occurrence": 7,
            "composition_turn": {
                "session_id": "privacy-session",
                "run_id": "run-1",
                "scope_id": "turn-7",
                "occurrence": 7,
                "invoked_call_ids": [f"call-{index}" for index in range(60)],
            },
            "run_id": "run-1",
        },
        "display_label": "visible",
        "api_key": "private-key",
        "long_value": "x" * 2000,
    }
    original = deepcopy(metadata)
    durable = session_metadata_for_persistence(metadata)
    assert durable["phase_records"] == original["phase_records"]
    assert durable["composition_turn_occurrence"] == 7
    assert durable["runtime_state"] == original["runtime_state"]
    assert session_metadata_for_persistence(durable)["phase_records"] == original["phase_records"]
    assert session_metadata_for_persistence(durable)["runtime_state"] == original["runtime_state"]
    session = SessionState(session=SessionRef("privacy-session"), metadata=durable)
    durable_before = deepcopy(durable)
    snapshot = RuntimeSessionDebugSnapshot(
        session=session,
        prompt="prompt",
        persisted_status="idle",
        current_status="idle",
        active=False,
        resumable=False,
        replayable=True,
        terminal=False,
    )
    emitter = _SessionStateEmitter()
    consumers = (
        RuntimeTransportApp._serialize_runtime_response(RuntimeResponse(session=session)),
        RuntimeTransportApp._serialize_runtime_stream_chunk(
            RuntimeStreamChunk(kind="session", session=session),
            session=emitter.serialize(session),
        ),
        serialize_session_debug_snapshot(snapshot),
        {"session": serialize_session_state(session)},
    )
    for payload in consumers:
        public_session = payload["session"]
        assert isinstance(public_session, dict)
        public_metadata = public_session["metadata"]
        assert isinstance(public_metadata, dict)
        assert "phase_records" not in public_metadata
        assert "composition_turn_occurrence" not in public_metadata
        public_runtime_state = public_metadata["runtime_state"]
        assert isinstance(public_runtime_state, dict)
        assert public_runtime_state == {"run_id": "run-1"}
        public_long_value = public_metadata["long_value"]
        assert isinstance(public_long_value, str)
        assert public_long_value.startswith("x" * 1000)
        assert len(public_long_value) < 2000
        assert public_metadata["api_key"] == "<redacted>"
        assert public_metadata["display_label"] == "visible"
        public_metadata["display_label"] = "changed by consumer"
    assert metadata == original
    assert durable == durable_before
