"""Write-time bounds on continuation/wakeup payloads at the services layer.

The store enforces the same bounds at the row (see test_storage.py); these tests prove
the services API refuses oversized arguments with `ValueError` before staging anything,
while keyed retries still name their existing row without restaging.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from voidcode.core.plugins import LeaseContext, PluginRuntime
from voidcode.core.services import RuntimeServices
from voidcode.core.storage.store import (
    BACKGROUND_ACTION_SOURCE,
    MAX_CONTINUATION_PAYLOAD_BYTES,
    MAX_WAKEUP_PAYLOAD_BYTES,
    SqliteStore,
    canonical_json,
)


def _payload_of_size(size: int) -> dict[str, object]:
    """Build a payload whose canonical JSON is exactly `size` bytes (`{"pad":"<pad>"}`)."""
    pad = size - len('{"pad":""}')
    assert pad >= 0
    return {"pad": "x" * pad}


@contextmanager
def _services(tmp_path: Path) -> Generator[tuple[RuntimeServices, LeaseContext, SqliteStore]]:
    with SqliteStore(tmp_path / "services-bounds.sqlite3") as store:
        rev_id = store.put_revision({"plugins": ["probe"]})
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        store.record_workspace_revision(str(workspace), rev_id)
        store.create_session("sess-probe", str(workspace), rev_id)
        with PluginRuntime((), workspace=workspace, revision_id=rev_id, store=store) as runtime:
            lease = runtime._open_lease(
                owner="probe",
                session_id="sess-probe",
                run_id="run-1",
                invocation_id="inv-1",
                readonly=False,
                actor="model",
                root_action_id="root-1",
            )
            yield RuntimeServices(runtime, lease), lease, store


def test_enqueue_continuation_rejects_oversized_payload_before_staging(tmp_path: Path) -> None:
    """An oversized continuation fails visibly at the API boundary, staging nothing."""
    payload = _payload_of_size(MAX_CONTINUATION_PAYLOAD_BYTES + 1)
    with _services(tmp_path) as (services, lease, _store):
        with pytest.raises(ValueError, match=f"{MAX_CONTINUATION_PAYLOAD_BYTES + 1} bytes.*{MAX_CONTINUATION_PAYLOAD_BYTES}-byte"):
            services.enqueue_continuation(payload)
        assert lease.staged_continuations == []
        assert lease.op_counter == 0


def test_enqueue_continuation_accepts_payload_at_bound(tmp_path: Path) -> None:
    """A continuation payload of exactly the bound stages for commit."""
    payload = _payload_of_size(MAX_CONTINUATION_PAYLOAD_BYTES)
    with _services(tmp_path) as (services, lease, _store):
        cid = services.enqueue_continuation(payload)
        assert cid
        (draft,) = lease.staged_continuations
        assert draft.continuation_id == cid
        assert draft.payload == payload


def test_keyed_retry_names_existing_row_without_restaging(tmp_path: Path) -> None:
    """The bound check runs after dedupe: a keyed retry names its row, never a new write."""
    with _services(tmp_path) as (services, lease, _store):
        first = services.enqueue_continuation({"note": "small"}, idempotency_key="k1")
        oversized = _payload_of_size(MAX_CONTINUATION_PAYLOAD_BYTES + 1)
        assert services.enqueue_continuation(oversized, idempotency_key="k1") == first
        assert len(lease.staged_continuations) == 1


def test_enqueue_background_action_rejects_oversized_action(tmp_path: Path) -> None:
    """The bound measures the wrapped row payload, so an oversized action fails before staging."""
    inner = _payload_of_size(MAX_CONTINUATION_PAYLOAD_BYTES + 1)
    outer_size = len(canonical_json({"action": inner, "root_action_budget": None, "tool_wait_authority": None}).encode("utf-8"))
    assert outer_size > MAX_CONTINUATION_PAYLOAD_BYTES
    with _services(tmp_path) as (services, lease, _store):
        with pytest.raises(ValueError, match=f"{outer_size} bytes.*{MAX_CONTINUATION_PAYLOAD_BYTES}-byte"):
            services.enqueue_background_action(inner)
        assert lease.staged_continuations == []


def test_enqueue_background_action_accepts_wrapped_payload_at_bound(tmp_path: Path) -> None:
    """A background action whose wrapped row payload is exactly the bound stages for commit."""
    fixed = len(canonical_json({"action": {}, "root_action_budget": None, "tool_wait_authority": None}).encode("utf-8")) - len(b"{}")
    inner = _payload_of_size(MAX_CONTINUATION_PAYLOAD_BYTES - fixed)
    outer = {"action": inner, "root_action_budget": None, "tool_wait_authority": None}
    assert len(canonical_json(outer).encode("utf-8")) == MAX_CONTINUATION_PAYLOAD_BYTES
    with _services(tmp_path) as (services, lease, _store):
        action_id = services.enqueue_background_action(inner)
        (draft,) = lease.staged_continuations
        assert draft.continuation_id == action_id
        assert draft.source == BACKGROUND_ACTION_SOURCE
        assert len(canonical_json(draft.payload).encode("utf-8")) == MAX_CONTINUATION_PAYLOAD_BYTES


def test_schedule_wakeup_rejects_oversized_payload(tmp_path: Path) -> None:
    """An oversized wakeup fails visibly at the API boundary and schedules nothing."""
    payload = _payload_of_size(MAX_WAKEUP_PAYLOAD_BYTES + 1)
    with _services(tmp_path) as (services, _lease, store):
        with pytest.raises(ValueError, match=f"{MAX_WAKEUP_PAYLOAD_BYTES + 1} bytes.*{MAX_WAKEUP_PAYLOAD_BYTES}-byte"):
            services.schedule_wakeup(payload, due_at_ms=0)
        assert store.pending_wakeups() == ()


def test_schedule_wakeup_accepts_payload_at_bound(tmp_path: Path) -> None:
    """A wakeup payload of exactly the bound schedules and reads back unchanged."""
    payload = _payload_of_size(MAX_WAKEUP_PAYLOAD_BYTES)
    with _services(tmp_path) as (services, _lease, store):
        wid = services.schedule_wakeup(payload, due_at_ms=0)
        (record,) = store.pending_wakeups()
        assert record.wakeup_id == wid
        assert record.payload == payload
