from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from voidcode.core.engine import TurnEngine
from voidcode.core.event_store import FactStore, MemoryEventStore
from voidcode.core.memory_host import MemoryAbortSignal, MemoryContext, MemoryHost
from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import ToolResultView, tool_result_output
from voidcode.core.turns import CallSeed, LoopStepFact, StreamFact, ToolCompletedFact, ToolRequestedFact, TurnPlan, TurnRequest, TurnSession
from voidcode.provider.protocol import ProviderStreamEvent
from voidcode.runtime.bundle import (
    SessionBundleError,
    apply_session_bundle,
    build_session_bundle,
    read_session_bundle_bytes,
    serialize_session_bundle,
)
from voidcode.runtime.contracts import RuntimeSessionCheckoutBoundaryError, RuntimeSessionForkBoundaryError
from voidcode.runtime.execution.turn_recovery import persisted_turn_batch
from voidcode.runtime.fact_store import SqliteFactStore
from voidcode.runtime.session_metadata_helpers import session_metadata_with_runtime_state_updates
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall, ToolResult
from voidcode.tools.read import ReadTool


@pytest.fixture(params=("memory", "sqlite"))
def facts(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryEventStore | SqliteFactStore:
    if request.param == "memory":
        return MemoryEventStore()
    owner = SqliteSessionStore(database_path=tmp_path / "facts.sqlite3")
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="facts",
        prompt="fact tree",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    return SqliteFactStore(events=owner, branches=owner, recovery=owner, workspace=tmp_path, session_id="facts")


def test_pinned_page_survives_checkout_and_retained_tail(facts: FactStore) -> None:
    facts.append((LoopStepFact(1, "plan"), LoopStepFact(2, "plan"), LoopStepFact(3, "finalize")))
    first = facts.read(limit=1)
    facts.checkout(1)
    assert facts.append((LoopStepFact(4, "plan"),)) == (4,)
    pinned = facts.read(after_sequence=first.next_after_sequence or 0, limit=2, leaf_sequence=first.leaf_sequence)
    assert [(entry.sequence, entry.parent_sequence) for entry in pinned.entries] == [(2, 1), (3, 2)]
    assert pinned.leaf_sequence == 3 and pinned.max_sequence == 4
    active = facts.read(limit=8)
    assert [(entry.sequence, entry.parent_sequence) for entry in active.entries] == [(1, None), (4, 1)]
    assert active.next_after_sequence is None


def test_atomic_live_fact_rejection_does_not_claim_dedupe_or_sequence(facts: FactStore) -> None:
    with pytest.raises(ValueError):
        facts.append(
            (LoopStepFact(1, "plan"), StreamFact(ProviderStreamEvent(kind="delta", channel="text", text="live"))),
            dedupe_keys=("first", "live"),
        )
    assert facts.read(limit=1).max_sequence == 0
    assert facts.append((LoopStepFact(1, "plan"),), dedupe_keys=("first",)) == (1,)
    assert facts.append((LoopStepFact(2, "plan"),), dedupe_keys=("first",)) == ()
    assert facts.append((LoopStepFact(3, "plan"),), dedupe_keys=("next",)) == (2,)


def test_memory_snapshots_keep_original_nested_arguments_and_isolate_readers() -> None:
    facts = MemoryEventStore()
    arguments: dict[str, object] = {"nested": {"items": ["original"]}}
    call = ToolCall("read", arguments, tool_call_id="native-a")
    facts.append((ToolRequestedFact(call),))
    arguments["nested"] = {"items": ["changed"]}
    first = facts.read(limit=1).entries[0].fact
    assert isinstance(first, ToolRequestedFact)
    assert first.call.arguments == {"nested": {"items": ["original"]}}
    first.call.arguments["nested"] = {"items": ["reader mutation"]}
    second = facts.read(limit=1).entries[0].fact
    assert isinstance(second, ToolRequestedFact)
    assert second.call.arguments == {"nested": {"items": ["original"]}}


def test_checkout_cannot_split_real_native_request_result_pair(facts: FactStore) -> None:
    call = ToolCall("read", {"path": "a.txt"}, tool_call_id="native-a")
    facts.append((LoopStepFact(1, "plan"), ToolRequestedFact(call), ToolCompletedFact(call, ToolResult("read", "ok", content="actual body"))))
    with pytest.raises((ValueError, RuntimeError)):
        facts.checkout(2)
    assert facts.read(limit=4).leaf_sequence == 3
    facts.checkout(3)
    completed = facts.read(limit=4).entries[-1].fact
    assert isinstance(completed, ToolCompletedFact)
    assert completed.call.tool_call_id == "native-a" and completed.result.content == "actual body"
    facts.append((ToolRequestedFact(call),))
    with pytest.raises((ValueError, RuntimeError)):
        facts.checkout(4)
    with pytest.raises((ValueError, RuntimeError)):
        facts.fork(4)
    assert facts.read(limit=8).leaf_sequence == 4
    facts.append((ToolCompletedFact(call, ToolResult("read", "ok", content="second actual body")),))
    assert facts.fork(5).read(limit=8).leaf_sequence == 5


def test_fork_preserves_gapped_active_edges_watermark_and_fresh_delivery_scope(facts: MemoryEventStore | SqliteFactStore) -> None:
    facts.append((LoopStepFact(1, "plan"), LoopStepFact(2, "plan"), LoopStepFact(3, "finalize")), dedupe_keys=("original", None, None))
    facts.checkout(1)
    facts.append((LoopStepFact(4, "plan"),))
    branch = facts.fork(4)
    page = branch.read(limit=8)
    assert [(entry.sequence, entry.parent_sequence) for entry in page.entries] == [(1, None), (4, 1)]
    assert page.leaf_sequence == 4 and page.max_sequence == 4
    assert branch.append((LoopStepFact(5, "finalize"),), dedupe_keys=("original",)) == (5,)
    assert facts.read(limit=8).max_sequence == 4
    facts.checkout(1)
    latest = facts.fork().read(limit=8)
    assert latest.leaf_sequence == 4 and latest.max_sequence == 4
    assert [entry.sequence for entry in latest.entries] == [1, 4]


@pytest.mark.parametrize("generation", ("codec", "sqlite", "bundle"))
def test_unsupported_generation_cannot_mutate_existing_log(tmp_path: Path, generation: str) -> None:
    database = tmp_path / "facts.sqlite3"
    owner = SqliteSessionStore(database_path=database)
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="facts",
        prompt="original request",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    facts = SqliteFactStore(events=owner, workspace=tmp_path, session_id="facts")
    facts.append((LoopStepFact(1, "plan"),))
    before = facts.read(limit=8)
    checkpoint = owner.load_resume_checkpoint(workspace=tmp_path, session_id="facts")
    if generation == "codec":
        with pytest.raises(ValueError):
            SqliteFactStore(events=owner, workspace=tmp_path, session_id="facts", codec_version=2).append((LoopStepFact(2, "plan"),))
    elif generation == "sqlite":
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version = 2")
        with pytest.raises(RuntimeError):
            facts.append((LoopStepFact(2, "plan"),))
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version = 1")
    else:
        bundle = build_session_bundle(sessions=owner, tasks=owner, workspace=tmp_path, session_id="facts")
        payload = json.loads(serialize_session_bundle(bundle, fmt="json"))
        payload["manifest"]["schema_version"] = 2
        with pytest.raises(SessionBundleError):
            apply_session_bundle(
                read_session_bundle_bytes(json.dumps(payload).encode()),
                session_repository=owner,
                events=owner,
                recovery=owner,
                run_writer=owner,
                workspace=tmp_path,
            )
    reopened = SqliteSessionStore(database_path=database)
    assert SqliteFactStore(events=reopened, workspace=tmp_path, session_id="facts").read(limit=8) == before
    assert reopened.load_resume_checkpoint(workspace=tmp_path, session_id="facts") == checkpoint
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT session_id FROM sessions").fetchall() == [("facts",)]


def test_deduped_atomic_checkpoint_cannot_resume_retained_orphan_branch(tmp_path: Path) -> None:
    owner = SqliteSessionStore(database_path=tmp_path / "facts.sqlite3")
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="facts",
        prompt="original branch",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    facts = SqliteFactStore(events=owner, branches=owner, workspace=tmp_path, session_id="facts")
    facts.append((LoopStepFact(1, "plan"), LoopStepFact(2, "plan"), LoopStepFact(3, "finalize")), dedupe_keys=("root", "abandoned", None))
    facts.checkout(1)
    checkpoint = owner.load_resume_checkpoint(workspace=tmp_path, session_id="facts")
    assert checkpoint is not None
    assert (
        facts.append_for_publication(
            (LoopStepFact(2, "plan"),),
            dedupe_keys=("abandoned",),
            interrupted_checkpoint={**checkpoint, "prompt": "retained orphan must not replace this branch"},
        )
        == ()
    )
    after = owner.load_resume_checkpoint(workspace=tmp_path, session_id="facts")
    assert after is not None and after == checkpoint
    owner.restore_leaf_after_interrupted_resume(workspace=tmp_path, session_id="facts", sequence=cast(int, after["last_event_sequence"]))
    resumed = facts.read(limit=8)
    assert resumed.leaf_sequence == 1 and resumed.max_sequence == 3
    assert [(entry.sequence, entry.parent_sequence) for entry in resumed.entries] == [(1, None)]
    committed = facts.append_for_publication((LoopStepFact(4, "plan"),), interrupted_checkpoint=checkpoint)
    refreshed = owner.load_resume_checkpoint(workspace=tmp_path, session_id="facts")
    assert refreshed is not None
    owner.restore_leaf_after_interrupted_resume(workspace=tmp_path, session_id="facts", sequence=cast(int, refreshed["last_event_sequence"]))
    assert committed[0].sequence == 4
    assert [(entry.sequence, entry.parent_sequence) for entry in facts.read(limit=8).entries] == [(1, None), (4, 1)]


def test_fork_pair_guard_uses_selected_path_not_retained_abandoned_request(facts: MemoryEventStore | SqliteFactStore) -> None:
    facts.append((LoopStepFact(1, "plan"), ToolRequestedFact(ToolCall("read", {"path": "abandoned.txt"}, "abandoned-native"))))
    facts.checkout(1)
    facts.append((LoopStepFact(2, "finalize"),))
    branch = facts.fork(3)
    assert [(entry.sequence, entry.parent_sequence) for entry in branch.read(limit=8).entries] == [(1, None), (3, 1)]
    assert facts.read(limit=8).max_sequence == 3


def test_governed_runtime_start_closes_original_native_identity_without_name_matching(tmp_path: Path) -> None:
    owner = SqliteSessionStore(database_path=tmp_path / "facts.sqlite3")
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="facts",
        prompt="rewritten native call",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    facts = SqliteFactStore(events=owner, branches=owner, workspace=tmp_path, session_id="facts")
    original = ToolCall("read", {"path": "original.txt"}, "provider-original")
    facts.append((ToolRequestedFact(original),))
    owner.append_session_events(
        workspace=tmp_path,
        session_id="facts",
        events=(
            (
                "runtime.tool_started",
                "runtime",
                {"tool": "rewritten", "arguments": {"path": "authorized.txt"}, "tool_call_id": original.tool_call_id},
                None,
            ),
        ),
    )
    facts.append(
        (
            ToolCompletedFact(
                ToolCall("rewritten", {"path": "authorized.txt"}, original.tool_call_id),
                ToolResult("rewritten", "ok", content="authorized result"),
            ),
        )
    )
    for split in (1, 2):
        with pytest.raises(RuntimeSessionCheckoutBoundaryError):
            facts.checkout(split)
        with pytest.raises(RuntimeSessionForkBoundaryError):
            facts.fork(split)
    facts.checkout(3)
    assert facts.fork(3).read(limit=8).leaf_sequence == 3


def test_legacy_missing_native_identity_refuses_branch_continuation_without_mutation(tmp_path: Path) -> None:
    owner = SqliteSessionStore(database_path=tmp_path / "facts.sqlite3")
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="facts",
        prompt="legacy history",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    owner.append_session_events(
        workspace=tmp_path,
        session_id="facts",
        events=(
            ("graph.loop_step", "graph", {"step": 1, "phase": "plan"}, None),
            ("runtime.tool_started", "runtime", {"tool": "read"}, None),
            ("runtime.tool_completed", "runtime", {"tool": "read", "status": "ok"}, None),
        ),
    )
    before = owner.read_session_event_page(workspace=tmp_path, session_id="facts", after_sequence=0, limit=8)
    with pytest.raises(RuntimeSessionCheckoutBoundaryError):
        owner.checkout_session(workspace=tmp_path, session_id="facts", sequence=3)
    with pytest.raises(RuntimeSessionForkBoundaryError):
        owner.fork_session(workspace=tmp_path, session_id="facts", at_sequence=3)
    assert owner.read_session_event_page(workspace=tmp_path, session_id="facts", after_sequence=0, limit=8) == before


@pytest.mark.parametrize("spoof_result_id", (False, True))
def test_recorded_real_reader_prefix_restores_without_repeating_completed_io(
    facts: MemoryEventStore | SqliteFactStore,
    tmp_path: Path,
    spoof_result_id: bool,
) -> None:
    (tmp_path / "first.txt").write_text("first real file body", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second real file body", encoding="utf-8")
    calls = (
        ToolCall("read", {"path": "first.txt"}, "native-first-read"),
        ToolCall("read", {"path": "second.txt"}, "native-second-read"),
    )
    reasoning = "Original nonsecret native batch reasoning."
    abort = MemoryAbortSignal()

    class Reader:
        definition = ReadTool.definition

        def __init__(self) -> None:
            self.call_ids: list[str | None] = []
            self.delegate = ReadTool()

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            self.call_ids.append(call.tool_call_id)
            result = self.delegate.invoke(call, context=replace(context, workspace=tmp_path))
            if len(self.call_ids) == 1:
                abort.set_cancelled(True)
                if spoof_result_id:
                    return replace(result, data={**result.data, "tool_call_id": "different-native-result"})
            return result

    class Producer:
        replayed_reasoning: str | None = None

        def produce(self, request: TurnRequest, tool_results: tuple[ToolResult | ToolResultView, ...], *, session: TurnSession) -> TurnPlan:
            if not tool_results:
                return TurnPlan(tool_calls=calls, reasoning=reasoning)
            context = request.assembled_context
            assert isinstance(context, MemoryContext)
            metadata = next(segment.metadata for segment in context.segments if segment.role == "tool")
            assert metadata is not None
            data = metadata["data"]
            assert isinstance(data, dict)
            replayed_reasoning = data["reasoning_content"]
            assert isinstance(replayed_reasoning, str)
            self.replayed_reasoning = replayed_reasoning
            return TurnPlan(output=tool_result_output(tool_results[-1]) or "", is_finished=True)

    reader = Reader()
    producer = Producer()
    host = MemoryHost(tools=(reader,), abort_signal=abort, event_store=facts)
    engine = TurnEngine(producer)
    if spoof_result_id:
        with pytest.raises(ValueError):
            list(engine.run(host.request("read both real files"), host=host))
        assert reader.call_ids == ["native-first-read"]
        assert not any(isinstance(entry.fact, ToolCompletedFact) for entry in facts.read(limit=100).entries)
        with pytest.raises(ValueError):
            facts.restore_batch()
        return
    list(engine.run(host.request("read both real files"), host=host))
    assert reader.call_ids == ["native-first-read"]
    if isinstance(facts, SqliteFactStore):
        reopened = SqliteSessionStore(database_path=tmp_path / "facts.sqlite3")
        facts = SqliteFactStore(events=reopened, recovery=reopened, workspace=tmp_path, session_id="facts")
    seed = facts.restore_batch()
    assert [call.tool_call_id for call in seed.calls] == ["native-first-read", "native-second-read"]
    assert seed.reasoning == reasoning and len(seed.completed_results) == 1
    assert tool_result_output(seed.completed_results[0]) == "first real file body"
    resumed = MemoryHost(tools=(reader,), event_store=facts)
    list(engine.run(resumed.request("read both real files"), host=resumed, seed=seed))
    assert reader.call_ids == ["native-first-read", "native-second-read"]
    assert producer.replayed_reasoning == reasoning
    restored = facts.restore_batch()
    assert [tool_result_output(result) for result in restored.completed_results] == ["first real file body", "second real file body"]


def test_runtime_only_page_does_not_erase_authentic_completed_prefix(tmp_path: Path) -> None:
    (tmp_path / "page.txt").write_text("real body beyond the empty typed page", encoding="utf-8")
    call = ToolCall("read", {"path": "page.txt"}, "native-after-runtime-page")
    batch = CallSeed((call,), reasoning="Original page-spanning reasoning.", run_step=1)
    metadata = session_metadata_with_runtime_state_updates(
        {},
        updates={
            "turn_batch": persisted_turn_batch(batch, session_id="facts", run_id=None, started_sequence=0),
        },
    )
    database = tmp_path / "facts.sqlite3"
    owner = SqliteSessionStore(database_path=database)
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="facts",
        prompt="read beyond runtime-only page",
        session_metadata=metadata,
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    owner.append_session_events(
        workspace=tmp_path,
        session_id="facts",
        events=tuple(("runtime.request_received", "runtime", {"prompt": f"runtime-only row {index}"}, None) for index in range(100)),
    )
    actual = ReadTool().invoke(call, context=ToolContext(workspace=tmp_path, session_id="facts", invocation_id=call.tool_call_id))
    facts = SqliteFactStore(events=owner, recovery=owner, workspace=tmp_path, session_id="facts")
    facts.append((ToolRequestedFact(call), ToolCompletedFact(call, actual, batch=batch)))
    reopened = SqliteSessionStore(database_path=database)
    restored_store = SqliteFactStore(events=reopened, recovery=reopened, workspace=tmp_path, session_id="facts")
    empty = restored_store.read(limit=100)
    assert empty.entries == () and empty.next_after_sequence == 100
    tail = restored_store.read(after_sequence=empty.next_after_sequence, limit=2, leaf_sequence=empty.leaf_sequence)
    assert [(entry.sequence, entry.parent_sequence) for entry in tail.entries] == [(101, 100), (102, 101)]
    restored = restored_store.restore_batch()
    assert restored.reasoning == batch.reasoning
    assert [result.data["tool_call_id"] for result in restored.completed_results] == [call.tool_call_id]
    assert tool_result_output(restored.completed_results[0]) == "real body beyond the empty typed page"


def test_stale_metadata_and_checkpoint_cannot_drop_or_resurrect_consumed_input(tmp_path: Path) -> None:
    owner = SqliteSessionStore(database_path=tmp_path / "queue.sqlite3")
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="queue",
        prompt="original",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    stale = owner.load_session(workspace=tmp_path, session_id="queue").session.metadata
    original = owner.load_resume_checkpoint(workspace=tmp_path, session_id="queue")
    assert original is not None
    key = "background-task-completion:actual-delivery"
    owner.enqueue_session_message(workspace=tmp_path, session_id="queue", content="new real input", kind="follow_up", dedupe_key=key)
    queued = owner.load_session(workspace=tmp_path, session_id="queue").session.metadata
    owner.update_session_metadata(workspace=tmp_path, session_id="queue", metadata={**stale, "actor": "new context"})
    facts = SqliteFactStore(events=owner, workspace=tmp_path, session_id="queue")
    facts.append_for_publication((LoopStepFact(1, "plan"),), interrupted_checkpoint=original)
    durable = owner.load_resume_checkpoint(workspace=tmp_path, session_id="queue")
    assert durable is not None
    checkpoint_metadata = cast(dict[str, object], durable["session_metadata"])
    checkpoint_messages = cast(list[dict[str, object]], checkpoint_metadata["pending_messages"])
    assert [message["content"] for message in checkpoint_messages] == ["new real input"]
    delivered = owner.drain_session_messages(workspace=tmp_path, session_id="queue", kind="follow_up", remember_dedupe=True)
    assert [message.content for message in delivered] == ["new real input"]
    owner.update_session_metadata(workspace=tmp_path, session_id="queue", metadata=queued)
    facts.append_for_publication((LoopStepFact(2, "plan"),), interrupted_checkpoint={**original, "session_metadata": queued})
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="queue",
        prompt="still original",
        session_metadata=queued,
        tool_results=(),
        last_event_sequence=2,
        create_if_missing=False,
    )
    reopened = SqliteSessionStore(database_path=tmp_path / "queue.sqlite3")
    assert reopened.drain_session_messages(workspace=tmp_path, session_id="queue", kind="follow_up") == ()
    assert (
        reopened.enqueue_session_message(
            workspace=tmp_path,
            session_id="queue",
            content="duplicate delivery",
            kind="follow_up",
            dedupe_key=key,
        )
        == ()
    )


def test_concurrent_enqueue_and_drain_serialize_real_input_once(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    owner = SqliteSessionStore(database_path=database)
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="queue",
        prompt="original",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    owner.enqueue_session_message(workspace=tmp_path, session_id="queue", content="first input", kind="follow_up")
    entered, release, drain_started = threading.Event(), threading.Event(), threading.Event()

    class PausedEnqueueOwner(SqliteSessionStore):
        @staticmethod
        def _read_session_message_metadata(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> dict[str, object]:
            metadata = SqliteSessionStore._read_session_message_metadata(connection=connection, workspace=workspace, session_id=session_id)
            entered.set()
            assert release.wait(timeout=5)
            return metadata

    writer = PausedEnqueueOwner(database_path=database)
    drainer = SqliteSessionStore(database_path=database)
    delivered: list[str] = []
    failures: list[BaseException] = []

    def enqueue() -> None:
        try:
            writer.enqueue_session_message(workspace=tmp_path, session_id="queue", content="second input", kind="follow_up")
        except BaseException as exc:
            failures.append(exc)

    def drain() -> None:
        drain_started.set()
        try:
            delivered.extend(message.content for message in drainer.drain_session_messages(workspace=tmp_path, session_id="queue", kind="follow_up"))
        except BaseException as exc:
            failures.append(exc)

    enqueue_thread, drain_thread = threading.Thread(target=enqueue), threading.Thread(target=drain)
    enqueue_thread.start()
    assert entered.wait(timeout=5)
    drain_thread.start()
    assert drain_started.wait(timeout=5)
    release.set()
    enqueue_thread.join(timeout=5)
    drain_thread.join(timeout=5)
    assert not enqueue_thread.is_alive() and not drain_thread.is_alive() and not failures
    assert delivered == ["first input", "second input"]
    assert drainer.drain_session_messages(workspace=tmp_path, session_id="queue", kind="follow_up") == ()


def test_corrupt_owned_metadata_rejects_snapshot_without_mutating_durable_truth(tmp_path: Path) -> None:
    database = tmp_path / "corrupt-metadata.sqlite3"
    owner = SqliteSessionStore(database_path=database)
    owner.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="queue",
        prompt="original",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    owner.enqueue_session_message(workspace=tmp_path, session_id="queue", content="real unconsumed input", kind="follow_up")
    SqliteFactStore(events=owner, workspace=tmp_path, session_id="queue").append((LoopStepFact(1, "plan"),))
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT metadata_json FROM sessions WHERE session_id = 'queue'").fetchone()
        corrupt = json.dumps([json.loads(row[0])])
        connection.execute("UPDATE sessions SET metadata_json = ? WHERE session_id = 'queue'", (corrupt,))
        original_session = connection.execute(
            "SELECT metadata_json, resume_checkpoint_json, last_event_sequence, leaf_sequence FROM sessions WHERE session_id = 'queue'",
        ).fetchone()
        original_events = connection.execute("SELECT * FROM session_events ORDER BY sequence").fetchall()
    connection.close()
    with pytest.raises(ValueError):
        owner.update_session_metadata(workspace=tmp_path, session_id="queue", metadata={"replacement": "not allowed"})
    with sqlite3.connect(database) as reopened:
        assert (
            reopened.execute(
                "SELECT metadata_json, resume_checkpoint_json, last_event_sequence, leaf_sequence FROM sessions WHERE session_id = 'queue'",
            ).fetchone()
            == original_session
        )
        assert reopened.execute("SELECT * FROM session_events ORDER BY sequence").fetchall() == original_events
    reopened.close()
