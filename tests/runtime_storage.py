from __future__ import annotations

from typing import Any

from voidcode.runtime.storage import RuntimeRepositories


def repositories_for_test_store(store: Any) -> RuntimeRepositories:
    return RuntimeRepositories(
        events=store,
        sessions=store,
        run_writer=store,
        recovery=store,
        tasks=store,
        maintenance=store,
        process_persistence=store,
    )
