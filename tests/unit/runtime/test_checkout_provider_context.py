"""Replayed provider context follows the checked-out leaf.

Drives the real assembly path (store + runtime), not the pure projector: turn A
and B are appended through the store, the leaf is checked out to A's node, turn
C is appended, and the context the runtime would replay for the next run must
hold A but not the abandoned B. Checkout is a position change, so B's rows stay
in the table and checking back out to B succeeds.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from tests.runtime_composition import save_checkpoint
from tests.runtime_storage import repositories_for_test_store
from voidcode.core.transcript import output_text, project_report
from voidcode.core.turns import FinalTurn, ReportedCall, TurnRequest, TurnSession
from voidcode.runtime.composition import CompositionRef, SessionCompositionOwner
from voidcode.runtime.events import runtime_reasoning_part_payload
from voidcode.runtime.execution.report_codec import report_payload
from voidcode.runtime.service import RuntimeRequest, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import TextOutput, ToolSuccess

_SESSION_ID = "checkout-context"


class _FinishingTurnProducer:
    """A producer that finishes on the first turn, only used to persist a real metadata snapshot."""

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: TurnSession) -> FinalTurn:
        _ = request, tool_results, session
        return FinalTurn(output="done")


def _seed_session(store: SqliteSessionStore, *, workspace: Path) -> None:
    save_checkpoint(
        store,
        workspace=workspace,
        session_id=_SESSION_ID,
        prompt="seed",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )


def _append_prompt(store: SqliteSessionStore, *, workspace: Path, prompt: str, sequence: int) -> None:
    store.append_session_events(
        workspace=workspace,
        session_id=_SESSION_ID,
        events=(
            ("runtime.request_received", "runtime", {"prompt": prompt}, None),
            ("core.response_ready", "core", {"sequence": sequence, "summary": prompt}, None),
        ),
    )


def _replayed_user_prompts(runtime: VoidCodeRuntime, *, store: SqliteSessionStore, workspace: Path) -> list[str]:
    stored = store.load_session(workspace=workspace, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )
    return [segment.content for segment in segments if segment.role == "user"]


def _stored_sequences(database_path: Path) -> list[int]:
    with sqlite3.connect(database_path) as connection:
        return [row[0] for row in connection.execute("SELECT sequence FROM session_events ORDER BY sequence ASC")]


def test_replayed_context_follows_the_checked_out_leaf(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-context.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    database_path = store._resolve_database_path()
    _seed_session(store, workspace=tmp_path)

    _append_prompt(store, workspace=tmp_path, prompt="prompt A", sequence=2)
    _append_prompt(store, workspace=tmp_path, prompt="prompt B", sequence=4)
    store.append_session_events(
        workspace=tmp_path,
        session_id=_SESSION_ID,
        events=(("runtime.todo_updated", "runtime", {"phases": [{"name": "B", "tasks": []}], "revision": 1}, None),),
    )
    before = _replayed_user_prompts(runtime, store=store, workspace=tmp_path)
    assert before == ["prompt A", "prompt B"]
    assert _stored_sequences(database_path) == [1, 2, 3, 4, 5]

    # Check out to A's node — the turn-A request itself.
    assert store.checkout_session(workspace=tmp_path, session_id=_SESSION_ID, sequence=1) == 1
    _append_prompt(store, workspace=tmp_path, prompt="prompt C", sequence=6)

    after = _replayed_user_prompts(runtime, store=store, workspace=tmp_path)
    assert "prompt A" in after
    assert "prompt B" not in after

    # B's branch was abandoned, not deleted, and stays reachable.
    assert _stored_sequences(database_path) == [1, 2, 3, 4, 5, 6, 7]
    assert store.checkout_session(workspace=tmp_path, session_id=_SESSION_ID, sequence=5) == 5
    assert [event.sequence for event in store.session_path(workspace=tmp_path, session_id=_SESSION_ID)] == [1, 2, 3, 4, 5]
    restored = _replayed_user_prompts(runtime, store=store, workspace=tmp_path)
    assert restored == ["prompt A", "prompt B"]


def test_replay_omits_tool_call_without_model_turn(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-no-model-turn.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    _seed_session(store, workspace=tmp_path)
    call_id = "orphan-read-call"
    store.append_session_events(
        workspace=tmp_path,
        session_id=_SESSION_ID,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "read sample.txt"}, None),
            (
                "core.tool_request_created",
                "core",
                {"tool": "read", "tool_call_id": call_id, "arguments": {"path": "sample.txt"}},
                None,
            ),
            (
                "runtime.tool_completed",
                "runtime",
                {
                    "tool": "read",
                    "tool_call_id": call_id,
                    "content": "sample contents",
                    "status": "ok",
                    "sequence": 1,
                    "reported_call": report_payload(
                        ReportedCall(
                            call_id,
                            "read",
                            {"path": "sample.txt"},
                            ToolSuccess("read", output=TextOutput("sample contents")),
                        )
                    ),
                },
                None,
            ),
        ),
    )

    stored = store.load_session(workspace=tmp_path, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )

    assert [segment.content for segment in segments if segment.role == "user"] == ["read sample.txt"]
    assert not any(segment.role in {"assistant", "tool"} for segment in segments)


def _append_tool_turn(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    prompt: str,
    tool_content: str,
    sequence: int,
    run_step: int,
    reasoning_content: str | None = None,
) -> None:
    events: list[tuple[str, str, dict[str, object], None]] = [
        ("runtime.request_received", "runtime", {"prompt": prompt}, None),
    ]
    if reasoning_content is not None:
        events.append(("runtime.reasoning_part", "runtime", runtime_reasoning_part_payload(text=reasoning_content), None))
    events.append(
        (
            "core.model_turn",
            "core",
            {
                "run_step": run_step,
                "mode": "provider",
                "prompt": prompt,
                "provider": "openai",
                "model": "deepseek-v4-pro",
                "attempt": 0,
                "streaming": False,
                "tool_batch_calls": [
                    {"tool": "read", "tool_call_id": f"read-call-{sequence}", "arguments": {}},
                ],
                "tool_batch_reasoning": reasoning_content,
            },
            None,
        )
    )
    events.extend(
        (
            ("core.tool_request_created", "core", {"tool": "read", "tool_call_id": f"read-call-{sequence}", "arguments": {}}, None),
            ("runtime.tool_started", "runtime", {"tool": "read", "tool_call_id": f"read-call-{sequence}", "arguments": {}}, None),
            (
                "runtime.tool_completed",
                "runtime",
                {
                    "tool": "read",
                    "tool_call_id": f"read-call-{sequence}",
                    "content": tool_content,
                    "status": "ok",
                    "sequence": sequence,
                    "reported_call": report_payload(
                        ReportedCall(f"read-call-{sequence}", "read", {}, ToolSuccess("read", output=TextOutput(tool_content)))
                    ),
                },
                None,
            ),
        )
    )
    store.append_session_events(workspace=workspace, session_id=_SESSION_ID, events=tuple(events))


def _append_tool_batch(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    prompt: str,
    run_step: int,
    calls: tuple[tuple[str, str], ...],
    reasoning_content: str | None = None,
    include_request: bool = True,
    request_count: int | None = None,
) -> None:
    events: list[tuple[str, str, dict[str, object], None]] = []
    if include_request:
        events.append(("runtime.request_received", "runtime", {"prompt": prompt}, None))
    if reasoning_content is not None:
        events.append(("runtime.reasoning_part", "runtime", runtime_reasoning_part_payload(text=reasoning_content), None))
    events.append(
        (
            "core.model_turn",
            "core",
            {
                "run_step": run_step,
                "mode": "provider",
                "prompt": prompt,
                "provider": "openai",
                "model": "deepseek-v4-pro",
                "attempt": 0,
                "streaming": False,
                "tool_batch_calls": [{"tool": "read", "tool_call_id": call_id, "arguments": {"path": call_id}} for call_id, _tool_content in calls],
                "tool_batch_reasoning": reasoning_content,
            },
            None,
        )
    )
    for offset, (call_id, tool_content) in enumerate(calls[:request_count]):
        arguments = {"path": call_id}
        sequence = run_step * 100 + offset
        events.extend(
            (
                (
                    "core.tool_request_created",
                    "core",
                    {"tool": "read", "tool_call_id": call_id, "arguments": arguments},
                    None,
                ),
                (
                    "runtime.tool_completed",
                    "runtime",
                    {
                        "tool": "read",
                        "tool_call_id": call_id,
                        "content": tool_content,
                        "status": "ok",
                        "sequence": sequence,
                        "reported_call": report_payload(
                            ReportedCall(
                                call_id,
                                "read",
                                arguments,
                                ToolSuccess("read", output=TextOutput(tool_content)),
                            )
                        ),
                    },
                    None,
                ),
            )
        )
    store.append_session_events(workspace=workspace, session_id=_SESSION_ID, events=tuple(events))


def test_replay_keeps_multi_call_batches_open_and_scopes_reused_ids(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-multi-call.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    _seed_session(store, workspace=tmp_path)

    _append_tool_batch(
        store,
        workspace=tmp_path,
        prompt="multi-call replay",
        run_step=1,
        calls=(("shared-call", "first result"), ("second-call", "second result")),
        reasoning_content="First model-turn reasoning.",
    )
    _append_tool_batch(
        store,
        workspace=tmp_path,
        prompt="multi-call replay",
        run_step=2,
        calls=(("shared-call", "reused call id result"),),
        reasoning_content="Second model-turn reasoning.",
        include_request=False,
    )
    _append_tool_batch(
        store,
        workspace=tmp_path,
        prompt="fresh cycle",
        run_step=3,
        calls=(("shared-call", "fresh cycle result"),),
    )

    stored = store.load_session(workspace=tmp_path, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
        include_current_request=True,
    )
    assistant_calls = [segment for segment in segments if segment.role == "assistant" and segment.tool_call_id]
    tool_results = [segment for segment in segments if segment.role == "tool"]

    assert [segment.tool_call_id for segment in assistant_calls] == [
        "shared-call",
        "second-call",
        "shared-call",
        "shared-call",
    ]
    assert len(tool_results) == 4
    assert assistant_calls[0].tool_batch_id == assistant_calls[1].tool_batch_id
    assert assistant_calls[2].tool_batch_id != assistant_calls[0].tool_batch_id
    assert assistant_calls[3].tool_batch_id != assistant_calls[2].tool_batch_id
    assert [segment.metadata.get("data") for segment in tool_results] == [
        {"reasoning_content": "First model-turn reasoning."},
        {"reasoning_content": "First model-turn reasoning."},
        {"reasoning_content": "Second model-turn reasoning."},
        None,
    ]


def test_replay_rejects_duplicate_call_ids_within_one_model_turn(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-duplicate-call.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    _seed_session(store, workspace=tmp_path)
    _append_tool_batch(
        store,
        workspace=tmp_path,
        prompt="duplicate tool call",
        run_step=1,
        calls=(("duplicate-call", "first result"), ("duplicate-call", "second result")),
    )

    stored = store.load_session(workspace=tmp_path, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )

    assert not any(segment.role in {"assistant", "tool"} for segment in segments)


def test_replay_omits_interrupted_prefix_of_manifest_batch(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-incomplete-batch.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    _seed_session(store, workspace=tmp_path)
    _append_tool_batch(
        store,
        workspace=tmp_path,
        prompt="interrupted batch",
        run_step=1,
        calls=(("first-call", "first result"), ("second-call", "second result")),
        request_count=1,
    )

    stored = store.load_session(workspace=tmp_path, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )

    assert not any(segment.role in {"assistant", "tool"} for segment in segments)


def test_replay_omits_model_turn_without_authoritative_batch_manifest(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-no-manifest.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    _seed_session(store, workspace=tmp_path)
    call_id = "legacy-read"
    arguments = {"path": "legacy.txt"}
    report = ReportedCall(
        call_id,
        "read",
        arguments,
        ToolSuccess("read", output=TextOutput("legacy contents")),
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id=_SESSION_ID,
        events=(
            ("runtime.request_received", "runtime", {"prompt": "read legacy.txt"}, None),
            (
                "core.model_turn",
                "core",
                {
                    "run_step": 1,
                    "mode": "provider",
                    "prompt": "read legacy.txt",
                    "provider": "openai",
                    "model": "deepseek-v4-pro",
                    "attempt": 0,
                    "streaming": False,
                },
                None,
            ),
            (
                "core.tool_request_created",
                "core",
                {"tool": "read", "tool_call_id": call_id, "arguments": arguments},
                None,
            ),
            (
                "runtime.tool_completed",
                "runtime",
                {"reported_call": report_payload(report)},
                None,
            ),
        ),
    )

    stored = store.load_session(workspace=tmp_path, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )

    assert not any(segment.role in {"assistant", "tool"} for segment in segments)


def test_replayed_tool_result_retains_turn_reasoning_when_resuming_current_prompt(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-reasoning.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    _seed_session(store, workspace=tmp_path)

    _append_tool_turn(
        store,
        workspace=tmp_path,
        prompt="prompt with reasoning",
        tool_content="A result",
        sequence=1,
        run_step=1,
        reasoning_content="Inspecting the file before reading it.",
    )
    _append_tool_turn(
        store,
        workspace=tmp_path,
        prompt="prompt with reasoning",
        tool_content="B result",
        sequence=2,
        run_step=2,
    )

    stored = store.load_session(workspace=tmp_path, session_id=_SESSION_ID)
    segments = runtime.replayed_conversation_segments_for_existing_session(
        stored=stored,
        parent_session_id=None,
        include_current_request=True,
    )
    user_prompts = [segment for segment in segments if segment.role == "user"]
    assert [segment.content for segment in user_prompts] == ["prompt with reasoning"] * 2
    assert [segment.metadata["kind"] for segment in user_prompts] == ["prior_user_prompt", "current_user_prompt"]
    assistant_calls = [segment for segment in segments if segment.role == "assistant" and segment.tool_call_id]
    tool_results = [segment for segment in segments if segment.role == "tool"]
    assert len(assistant_calls) == len(tool_results) == 2
    assert assistant_calls[0].tool_batch_id != assistant_calls[1].tool_batch_id
    assert segments.index(user_prompts[0]) < segments.index(assistant_calls[0]) < segments.index(tool_results[0])
    assert segments.index(tool_results[0]) < segments.index(user_prompts[1]) < segments.index(assistant_calls[1])
    assert segments.index(assistant_calls[1]) < segments.index(tool_results[1])
    first_data = tool_results[0].metadata["data"]
    assert first_data == {"reasoning_content": "Inspecting the file before reading it."}
    assert "data" not in tool_results[1].metadata


def _rehydrated_tool_contents(runtime: VoidCodeRuntime, *, store: SqliteSessionStore, workspace: Path) -> list[str]:
    stored = store.load_session(workspace=workspace, session_id=_SESSION_ID)
    results = runtime._rehydrated_tool_results_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )
    return [output_text(project_report(result).output) or "" for result in results]


def test_rehydrated_tool_results_follow_the_checked_out_leaf(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-tool-results.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store))
    database_path = store._resolve_database_path()
    _seed_session(store, workspace=tmp_path)

    _append_tool_turn(
        store,
        workspace=tmp_path,
        prompt="prompt A",
        tool_content="A result",
        sequence=1,
        run_step=1,
    )
    _append_tool_turn(
        store,
        workspace=tmp_path,
        prompt="prompt B",
        tool_content="B result",
        sequence=3,
        run_step=2,
    )
    before = _rehydrated_tool_contents(runtime, store=store, workspace=tmp_path)
    assert before == ["A result", "B result"]
    assert _stored_sequences(database_path) == list(range(1, 11))

    # Check out to A's tool completion — B's turn is abandoned off the path.
    assert store.checkout_session(workspace=tmp_path, session_id=_SESSION_ID, sequence=5) == 5
    after = _rehydrated_tool_contents(runtime, store=store, workspace=tmp_path)
    assert after == ["A result"]


def _debug_provider_tool_contents(runtime: VoidCodeRuntime, *, workspace: Path) -> list[str]:
    snapshot = runtime.session_debug_snapshot(session_id=_SESSION_ID)
    assert snapshot.provider_context is not None
    return [str(segment.content) for segment in snapshot.provider_context.segments if segment.role == "tool" and segment.content]


def test_debug_provider_context_follows_the_checked_out_leaf(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-debug-context.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, repositories=repositories_for_test_store(store), turn_producer=_FinishingTurnProducer())

    # Harvest the full metadata snapshot a real run persists (runtime_config +
    # agent_capability_snapshot) so the debug read passes its boundary checks.
    seed = runtime.run(RuntimeRequest(prompt="seed", session_id="meta-seed"))

    metadata = dict(seed.session.metadata)
    composition = store.load_execution_composition(ref=CompositionRef.model_validate(metadata["composition_ref"]))
    composition_ref = composition.reference(
        workspace=str(tmp_path),
        owner=SessionCompositionOwner(kind="session", session_id=_SESSION_ID),
    )
    metadata["composition_ref"] = composition_ref.model_dump(mode="json")
    capability = dict(cast(dict[str, object], metadata["agent_capability_snapshot"]))
    capability["composition_ref"] = composition_ref.model_dump(mode="json")
    metadata["agent_capability_snapshot"] = capability
    runtime_state = dict(cast(dict[str, object], metadata["runtime_state"]))
    runtime_state.pop("composition_turn", None)
    runtime_state.pop("composition_turn_occurrence", None)
    runtime_state.pop("turn_batch", None)
    metadata["runtime_state"] = runtime_state
    metadata.pop("execution_composition", None)
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=_SESSION_ID,
        prompt="seed",
        session_metadata=metadata,
        tool_results=(),
        last_event_sequence=0,
        composition_ref=composition_ref,
        composition=composition,
        create_if_missing=True,
    )

    _append_tool_turn(
        store,
        workspace=tmp_path,
        prompt="prompt A",
        tool_content="A result",
        sequence=1,
        run_step=1,
    )
    _append_tool_turn(
        store,
        workspace=tmp_path,
        prompt="prompt B",
        tool_content="B result",
        sequence=3,
        run_step=2,
    )
    before = _debug_provider_tool_contents(runtime, workspace=tmp_path)
    assert "A result" in before
    assert "B result" in before

    # Check out to A's tool completion — B's turn is abandoned off the path, so
    # the debug snapshot must stop reporting B's tool result to the operator.
    assert store.checkout_session(workspace=tmp_path, session_id=_SESSION_ID, sequence=5) == 5
    after = _debug_provider_tool_contents(runtime, workspace=tmp_path)
    assert "A result" in after
    assert "B result" not in after

    # B's branch was abandoned, not deleted: checking back out restores it.
    assert [event.sequence for event in store.session_path(workspace=tmp_path, session_id=_SESSION_ID)] == list(range(1, 6))
    assert store.checkout_session(workspace=tmp_path, session_id=_SESSION_ID, sequence=10) == 10
    restored = _debug_provider_tool_contents(runtime, workspace=tmp_path)
    assert "A result" in restored
    assert "B result" in restored
