"""Owned core workers finish reading before their store/runtime is closed."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import SqliteStore, StorageError


@pytest.mark.parametrize("kind", ["activation", "ticker"])
def test_shutdown_keeps_store_open_until_owned_worker_exits(tmp_path, monkeypatch, kind):
    entered = threading.Event()
    release = threading.Event()
    joining = threading.Event()
    workers = []
    errors = []

    def blocked(core):
        workers.append(threading.current_thread())
        entered.set()
        try:
            assert release.wait(timeout=5)
            core.store.sessions(workspace=str(core.workspace))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(Core, "_drive_pending_activations", blocked if kind != "ticker" else lambda core: None)
    monkeypatch.setattr(Core, "_wakeup_loop", blocked if kind == "ticker" else lambda core: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    core = open_core(workspace, state_dir=tmp_path / "state")
    try:
        assert entered.wait(timeout=2)
        actual_join = workers[0].join

        def join(*args, **kwargs):
            joining.set()
            return actual_join(*args, **kwargs)

        monkeypatch.setattr(workers[0], "join", join)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(core.close)
            try:
                assert joining.wait(timeout=2)
                core.store.sessions(workspace=str(workspace))
                assert not future.done()
            finally:
                release.set()
            future.result(timeout=5)
        assert errors == []
        with pytest.raises(StorageError, match="store is closed"):
            core.store.sessions(workspace=str(workspace))
    finally:
        release.set()
        core.close()


def test_failed_start_joins_worker_before_closing_store_and_releases_workspace_lease(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []
    original_join = threading.Thread.join

    def blocked(core):
        entered.set()
        try:
            assert release.wait(timeout=5)
            core.store.sessions(workspace=str(core.workspace))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    def fail_record(*args, **kwargs):
        assert entered.wait(timeout=2)
        raise RuntimeError("injected revision recording failure")

    def join(worker, *args, **kwargs):
        if worker.name == "CoreActivationDriver":
            release.set()
        return original_join(worker, *args, **kwargs)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text('{"schema_version": 1}', encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(Core, "_drive_pending_activations", blocked)
        patch.setattr(Core, "_wakeup_loop", lambda core: None)
        patch.setattr(SqliteStore, "record_workspace_revision", fail_record)
        patch.setattr(threading.Thread, "join", join)
        try:
            with pytest.raises(RuntimeError, match="injected revision recording failure"):
                open_core(workspace, state_dir=tmp_path / "state")
        finally:
            release.set()
            assert finished.wait(timeout=2)
    assert errors == []
    reopened = open_core(workspace, state_dir=tmp_path / "state")
    reopened.close()
