"""Storage-level session fork: copied prefix, provenance, untouched source.

The behavior under test is the invariant the whole feature rests on: a fork is a
*new* linear session whose event log is a contiguous ``1..N`` copy of its
source's, whose watermark is its own ``N``, and whose source is byte-unchanged.
Everything else (list projection, CLI, tree walking) is a projection of those
rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from voidcode.runtime.contracts import (
    RuntimeSessionForkBoundaryError,
    SessionLineageCycleError,
    UnknownSessionError,
)
from voidcode.runtime.session import SessionRef, SessionState, StoredSessionLineageEntry
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.storage.fork import forest_from_lineage_entries


def _event_tuple(*rows: tuple[str, str, dict[str, object]]) -> tuple[tuple[str, str, dict[str, object], None], ...]:
    return tuple((event_type, source, payload, None) for event_type, source, payload in rows)


def _seed_session(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    session_id: str,
    prompt: str,
    events: tuple[tuple[str, str, dict[str, object], None], ...],
) -> None:
    """Write a session row plus its events the way the run loop would."""
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=session_id,
        prompt=prompt,
        session_metadata={"workspace": str(workspace), "runtime_state": {"todos": [{"id": "stale"}]}},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=workspace,
        session_id=session_id,
        events=events,
    )
    store.save_run(
        workspace=workspace,
        request=_request(session_id=session_id, prompt=prompt),
        response=_response(session_id=session_id, events=events),
    )


def _request(*, session_id: str, prompt: str):
    from voidcode.runtime.contracts import RuntimeRequest

    return RuntimeRequest(prompt=prompt, session_id=session_id)


def _response(*, session_id: str, events: tuple[tuple[str, str, dict[str, object], None], ...]):
    from voidcode.runtime.contracts import RuntimeResponse
    from voidcode.runtime.events import EventEnvelope

    envelopes = tuple(
        EventEnvelope(
            session_id=session_id,
            sequence=index + 1,
            event_type=event_type,
            source=source,
            payload=payload,
        )
        for index, (event_type, source, payload, _key) in enumerate(events)
    )
    return RuntimeResponse(
        session=SessionState(
            session=SessionRef(id=session_id, parent_id=None),
            status="completed",
            turn=1,
            metadata={"workspace": "x"},
        ),
        events=envelopes,
        output="done",
    )


def test_fork_copies_prefix_own_watermark_and_leaves_source_unchanged(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    events = _event_tuple(
        ("runtime.request_received", "runtime", {"prompt": "hello"}),
        ("graph.response_ready", "graph", {"summary": "done"}),
    )
    _seed_session(store, workspace=tmp_path, session_id="source-1", prompt="hello", events=events)

    before = store.load_session(workspace=tmp_path, session_id="source-1")
    forked = store.fork_session(workspace=tmp_path, session_id="source-1")

    assert forked.session.id != "source-1"
    assert forked.forked_from_session_id == "source-1"
    assert forked.forked_at_sequence == 2
    assert forked.session.parent_id is None  # NOT a delegated child

    copied = store.load_session(workspace=tmp_path, session_id=forked.session.id)
    assert [event.sequence for event in copied.events] == [1, 2]
    assert [event.event_type for event in copied.events] == [
        "runtime.request_received",
        "graph.response_ready",
    ]
    assert copied.session.session.parent_id is None

    # Watermark is the fork's own boundary, read back through the row.
    summaries = {summary.session.id: summary for summary in store.list_sessions(workspace=tmp_path)}
    assert summaries[forked.session.id].forked_at_sequence == 2

    # The source is untouched: same events, same status, same watermark.
    after = store.load_session(workspace=tmp_path, session_id="source-1")
    assert after.session == before.session
    assert [(event.sequence, event.event_type) for event in after.events] == [(event.sequence, event.event_type) for event in before.events]


def test_fork_refuses_boundary_that_splits_a_tool_call(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    events = _event_tuple(
        ("runtime.request_received", "runtime", {"prompt": "run"}),
        ("runtime.tool_started", "runtime", {"tool": "read", "tool_call_id": "call-1"}),
        ("runtime.tool_completed", "tool", {"tool": "read", "tool_call_id": "call-1", "status": "ok"}),
        ("graph.response_ready", "graph", {"summary": "done"}),
    )
    _seed_session(store, workspace=tmp_path, session_id="source-3", prompt="run", events=events)

    with pytest.raises(RuntimeSessionForkBoundaryError, match="splits a tool call"):
        store.fork_session(workspace=tmp_path, session_id="source-3", at_sequence=2)

    safe = store.fork_session(workspace=tmp_path, session_id="source-3", at_sequence=3)
    assert safe.forked_at_sequence == 3


def test_lineage_walks_fork_ancestry_oldest_first(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    events = _event_tuple(
        ("runtime.request_received", "runtime", {"prompt": "root"}),
        ("graph.response_ready", "graph", {"summary": "root"}),
    )
    _seed_session(store, workspace=tmp_path, session_id="root-1", prompt="root", events=events)
    child = store.fork_session(workspace=tmp_path, session_id="root-1")
    grandchild = store.fork_session(workspace=tmp_path, session_id=child.session.id)

    chain = store.session_lineage(workspace=tmp_path, session_id=grandchild.session.id)
    assert [entry.session_id for entry in chain] == [
        "root-1",
        child.session.id,
        grandchild.session.id,
    ]
    assert chain[-1].forked_from_session_id == child.session.id

    everything = store.session_lineage(workspace=tmp_path)
    assert {entry.session_id for entry in everything} >= {"root-1", child.session.id, grandchild.session.id}


def test_fork_unknown_session_raises(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    with pytest.raises(UnknownSessionError):
        store.fork_session(workspace=tmp_path, session_id="missing")


def test_fork_carries_session_config_but_drops_run_position_state(tmp_path: Path) -> None:
    """A fork keeps the session-scoped config/policy but not the run position.

    ``runtime_config``/``runtime_policy`` describe *the session*; without them
    ``run -r <fork>`` and inspection reject the row ("must include
    runtime_config"), so the fork would be created unusable. ``runtime_state``
    (todos + context projection) describes the copied run's position and must
    not leak into a session that does not own those events.
    """
    database_path = tmp_path / "fork-config.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "done"}))
    _seed_session(store, workspace=tmp_path, session_id="source-cfg", prompt="hi", events=events)
    # Patch the row directly: the point is what the fork copies, and the public
    # metadata writer validates runtime_policy against the full snapshot schema.
    with sqlite3.connect(database_path) as connection:
        raw = connection.execute("SELECT metadata_json FROM sessions WHERE session_id = 'source-cfg'").fetchone()[0]
        metadata = json.loads(raw)
        metadata["runtime_config"] = {"model": "deepseek/deepseek-chat"}
        metadata["runtime_policy"] = {"mode": "normal"}
        metadata["runtime_state"] = {"run_id": "stale-run"}
        connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = 'source-cfg'",
            (json.dumps(metadata),),
        )
        connection.commit()

    forked = store.fork_session(workspace=tmp_path, session_id="source-cfg")
    forked_metadata = store.load_session(workspace=tmp_path, session_id=forked.session.id).session.metadata

    assert forked_metadata["runtime_config"] == {"model": "deepseek/deepseek-chat"}
    assert forked_metadata["runtime_policy"] == {"mode": "normal"}
    assert "runtime_state" not in forked_metadata


def test_fork_provenance_survives_a_later_run_seal(tmp_path: Path) -> None:
    """Running a fork must not erase its lineage.

    ``save_run`` seals the row with ``INSERT OR REPLACE``, which rewrites every
    column: provenance is a row column the run snapshot does not own, so it has
    to be read back and carried forward the same way ``title`` is.
    """
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "done"}))
    _seed_session(store, workspace=tmp_path, session_id="source-run", prompt="hi", events=events)
    forked = store.fork_session(workspace=tmp_path, session_id="source-run", at_sequence=1)

    store.save_run(
        workspace=tmp_path,
        request=_request(session_id=forked.session.id, prompt="continued"),
        response=_response(session_id=forked.session.id, events=events),
    )

    reloaded = {summary.session.id: summary for summary in store.list_sessions(workspace=tmp_path)}[forked.session.id]
    assert reloaded.forked_from_session_id == "source-run"
    assert reloaded.forked_at_sequence == 1


def test_session_forest_keeps_the_parent_above_a_continued_fork(tmp_path: Path) -> None:
    """Depth must come from provenance edges, not from the rows' ``updated_at`` order.

    A parent continued *after* its forks has the newest ``updated_at`` and so
    sorts last in ``session_lineage``; deriving depth in that order collapses
    every fork to depth 1. The forest walk pins the parent at depth 0 and both
    forks at depth 1, parent first.
    """
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "root"}))
    _seed_session(store, workspace=tmp_path, session_id="root", prompt="root", events=events)
    first_fork = store.fork_session(workspace=tmp_path, session_id="root")
    second_fork = store.fork_session(workspace=tmp_path, session_id="root")
    # Continue the original parent so its row is now the most recently updated.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="root",
        prompt="root continued",
        session_metadata={"workspace": str(tmp_path)},
        tool_results=(),
        last_event_sequence=1,
        create_if_missing=True,
    )

    forest = store.session_forest(workspace=tmp_path)

    # Both forks share the same boundary, so they order by id behind the root.
    first_entry, *rest = forest
    assert first_entry.session_id == "root"
    assert first_entry.forked_from_session_id is None
    assert [entry.session_id for entry in rest] == sorted((first_fork.session.id, second_fork.session.id))
    assert [entry.depth for entry in forest] == [0, 1, 1]
    assert {entry.forked_from_session_id for entry in rest} == {"root"}


def test_session_forest_orders_siblings_and_grandchildren_parent_first(tmp_path: Path) -> None:
    """One depth-first order: root, its forks, then their children.

    Sibling order is the data-determined ``(forked_at_sequence, session_id)``,
    never ``updated_at``: both forks share the same boundary here, so they order
    by id. A grandchild always follows the fork it branched from, so the printed
    indentation is a valid tree.
    """
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "root"}))
    _seed_session(store, workspace=tmp_path, session_id="root", prompt="root", events=events)
    first_fork = store.fork_session(workspace=tmp_path, session_id="root", at_sequence=1)
    grandchild = store.fork_session(workspace=tmp_path, session_id=first_fork.session.id, at_sequence=1)
    second_fork = store.fork_session(workspace=tmp_path, session_id="root")

    forest = store.session_forest(workspace=tmp_path)

    # Depth-first: root, each fork in (sequence, id) order with its own subtree.
    expected: list[tuple[str, int]] = [("root", 0)]
    for sibling in sorted((first_fork.session.id, second_fork.session.id)):
        expected.append((sibling, 1))
        if sibling == first_fork.session.id:
            expected.append((grandchild.session.id, 2))

    assert [(entry.session_id, entry.depth) for entry in forest] == expected
    # Parents are always emitted before their children.
    positions = {entry.session_id: index for index, entry in enumerate(forest)}
    for entry in forest:
        if entry.forked_from_session_id is not None:
            assert positions[entry.forked_from_session_id] < positions[entry.session_id]


def test_session_forest_keeps_a_fork_of_a_deleted_parent_as_a_root() -> None:
    """A provenance edge to a session outside the returned set must not drop it."""
    forest = forest_from_lineage_entries(
        (
            StoredSessionLineageEntry(session_id="orphan", forked_from_session_id="deleted", forked_at_sequence=3, updated_at=1),
            StoredSessionLineageEntry(session_id="deleted-child", forked_from_session_id="orphan", forked_at_sequence=4, updated_at=2),
        )
    )

    assert [(entry.session_id, entry.depth) for entry in forest] == [("orphan", 0), ("deleted-child", 1)]
    assert forest[0].forked_from_session_id == "deleted"


def test_session_forest_excludes_delegated_children(tmp_path: Path) -> None:
    """A delegated child (``parent_session_id`` set) is not a fork node.

    Delegation and fork use separate columns; the forest describes fork
    provenance only, so a delegated child must not be classified as a root the
    way a plain ``NULL forked_from_session_id`` row would be. Every other
    surface (CLI/HTTP list) filters these out, so the forest must too.
    """
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "root"}))
    _seed_session(store, workspace=tmp_path, session_id="root", prompt="root", events=events)
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="delegated-child",
        prompt="child work",
        session_metadata={"workspace": str(tmp_path)},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
        parent_session_id="root",
    )

    forest = store.session_forest(workspace=tmp_path)

    assert [entry.session_id for entry in forest] == ["root"]
    # Only the whole-workspace forest read excludes delegated children; the
    # named-session ancestry walk still sees them.
    assert [entry.session_id for entry in store.session_lineage(workspace=tmp_path, session_id="delegated-child")] == ["delegated-child"]


def test_session_forest_keeps_a_fork_of_a_delegated_child_as_a_root(tmp_path: Path) -> None:
    """Excluding a delegated child leaves its fork an orphan root, not a gap.

    The fork's ``forked_from_session_id`` points at the excluded child, so the
    existing orphan rule applies: it stays visible as a root instead of
    vanishing from the tree.
    """
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "root"}))
    _seed_session(store, workspace=tmp_path, session_id="root", prompt="root", events=events)
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="delegated-child",
        prompt="child work",
        session_metadata={"workspace": str(tmp_path)},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
        parent_session_id="root",
    )
    store.append_session_events(workspace=tmp_path, session_id="delegated-child", events=events)
    fork_of_child = store.fork_session(workspace=tmp_path, session_id="delegated-child")

    forest = store.session_forest(workspace=tmp_path)

    # Both are roots at depth 0. The fork was created last, so recency puts it
    # first; the orphan rule still keeps it visible instead of dropping it.
    assert [(entry.session_id, entry.depth) for entry in forest] == [(fork_of_child.session.id, 0), ("root", 0)]
    assert forest[0].forked_from_session_id == "delegated-child"


def test_session_forest_breaks_root_and_sibling_ties_by_session_id() -> None:
    """Two independent roots (and equally sequenced siblings) order by id, not insertion."""
    forest = forest_from_lineage_entries(
        (
            StoredSessionLineageEntry(session_id="zeta", forked_from_session_id=None, forked_at_sequence=None, updated_at=0),
            StoredSessionLineageEntry(session_id="alpha", forked_from_session_id=None, forked_at_sequence=None, updated_at=0),
        )
    )

    assert [entry.session_id for entry in forest] == ["alpha", "zeta"]


def test_session_forest_rejects_a_provenance_cycle() -> None:
    """Malformed provenance has no root; the walk must refuse it, not hang."""
    with pytest.raises(SessionLineageCycleError, match="cycle"):
        forest_from_lineage_entries(
            (
                StoredSessionLineageEntry(session_id="a", forked_from_session_id="b", forked_at_sequence=1, updated_at=1),
                StoredSessionLineageEntry(session_id="b", forked_from_session_id="a", forked_at_sequence=2, updated_at=2),
            )
        )


def test_session_forest_orders_roots_by_recency_but_children_by_provenance(tmp_path: Path) -> None:
    """One display order: roots most-recently-active first, each subtree under its root.

    This is the reproduction of the reported defect. Ordinary flow: run a root,
    fork it, continue the *fork*. ``list_sessions`` is ``updated_at DESC``, so
    the fork is the newest row; a surface that renders that order below its
    parent's indent draws the child above the parent. Both roots here are
    independent (``first`` older than ``second``), and each has a continued
    fork, so recency must apply to the roots only.
    """
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "root"}))
    _seed_session(store, workspace=tmp_path, session_id="first", prompt="first", events=events)
    first_fork = store.fork_session(workspace=tmp_path, session_id="first")
    _seed_session(store, workspace=tmp_path, session_id="second", prompt="second", events=events)
    second_fork = store.fork_session(workspace=tmp_path, session_id="second")
    # Continue both forks, the second one last, so it is the newest row overall.
    for session_id in (first_fork.session.id, second_fork.session.id):
        store.save_interrupted_checkpoint(
            workspace=tmp_path,
            session_id=session_id,
            prompt="continued",
            session_metadata={"workspace": str(tmp_path)},
            tool_results=(),
            last_event_sequence=1,
            create_if_missing=True,
        )

    forest = store.session_forest(workspace=tmp_path)

    # Roots by recency ("second" is newer), each immediately followed by its fork.
    assert [(entry.session_id, entry.depth) for entry in forest] == [
        ("second", 0),
        (second_fork.session.id, 1),
        ("first", 0),
        (first_fork.session.id, 1),
    ]
    # No row precedes the parent it forked from, even though both forks are newer.
    positions = {entry.session_id: index for index, entry in enumerate(forest)}
    for entry in forest:
        if entry.forked_from_session_id is not None:
            assert positions[entry.forked_from_session_id] < positions[entry.session_id]


def test_session_forest_keeps_a_grandchild_under_a_continued_fork(tmp_path: Path) -> None:
    """A grandchild follows the fork it branched from, never the newest-row order."""
    store = SqliteSessionStore()
    events = _event_tuple(("graph.response_ready", "graph", {"summary": "root"}))
    _seed_session(store, workspace=tmp_path, session_id="root", prompt="root", events=events)
    fork = store.fork_session(workspace=tmp_path, session_id="root", at_sequence=1)
    grandchild = store.fork_session(workspace=tmp_path, session_id=fork.session.id, at_sequence=1)
    # Continue the middle fork after forking it: it is now newer than its child.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=fork.session.id,
        prompt="fork continued",
        session_metadata={"workspace": str(tmp_path)},
        tool_results=(),
        last_event_sequence=1,
        create_if_missing=True,
    )

    forest = store.session_forest(workspace=tmp_path)

    assert [(entry.session_id, entry.depth) for entry in forest] == [
        ("root", 0),
        (fork.session.id, 1),
        (grandchild.session.id, 2),
    ]


def test_session_forest_keeps_siblings_under_their_parent_when_one_is_newer(tmp_path: Path) -> None:
    """Two forks of one root stay siblings below it, ordered by boundary then id."""
    store = SqliteSessionStore()
    events = _event_tuple(
        ("runtime.request_received", "runtime", {"prompt": "root"}),
        ("graph.response_ready", "graph", {"summary": "root"}),
    )
    _seed_session(store, workspace=tmp_path, session_id="root", prompt="root", events=events)
    older = store.fork_session(workspace=tmp_path, session_id="root", at_sequence=1)
    younger = store.fork_session(workspace=tmp_path, session_id="root", at_sequence=2)

    forest = store.session_forest(workspace=tmp_path)

    assert [(entry.session_id, entry.depth, entry.forked_from_session_id) for entry in forest] == [
        ("root", 0, None),
        (older.session.id, 1, "root"),
        (younger.session.id, 1, "root"),
    ]
