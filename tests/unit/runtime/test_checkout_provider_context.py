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

from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore

_SESSION_ID = "checkout-context"


def _seed_session(store: SqliteSessionStore, *, workspace: Path) -> None:
    store.save_interrupted_checkpoint(
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
            ("graph.response_ready", "graph", {"sequence": sequence, "summary": prompt}, None),
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
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
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


def _append_tool_turn(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    prompt: str,
    tool_content: str,
    sequence: int,
) -> None:
    store.append_session_events(
        workspace=workspace,
        session_id=_SESSION_ID,
        events=(
            ("runtime.request_received", "runtime", {"prompt": prompt}, None),
            (
                "runtime.tool_completed",
                "runtime",
                {"tool": "read", "content": tool_content, "status": "ok", "sequence": sequence},
                None,
            ),
        ),
    )


def _rehydrated_tool_contents(runtime: VoidCodeRuntime, *, store: SqliteSessionStore, workspace: Path) -> list[str]:
    stored = store.load_session(workspace=workspace, session_id=_SESSION_ID)
    results = runtime._rehydrated_tool_results_for_existing_session(
        stored=stored,
        parent_session_id=None,
    )
    return [str(result.content) for result in results]


def test_rehydrated_tool_results_follow_the_checked_out_leaf(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "checkout-tool-results.sqlite3")
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
    database_path = store._resolve_database_path()
    _seed_session(store, workspace=tmp_path)

    _append_tool_turn(store, workspace=tmp_path, prompt="prompt A", tool_content="A result", sequence=1)
    _append_tool_turn(store, workspace=tmp_path, prompt="prompt B", tool_content="B result", sequence=3)
    before = _rehydrated_tool_contents(runtime, store=store, workspace=tmp_path)
    assert before == ["A result", "B result"]
    assert _stored_sequences(database_path) == [1, 2, 3, 4]

    # Check out to A's tool completion — B's turn is abandoned off the path.
    assert store.checkout_session(workspace=tmp_path, session_id=_SESSION_ID, sequence=2) == 2
    after = _rehydrated_tool_contents(runtime, store=store, workspace=tmp_path)
    assert after == ["A result"]
