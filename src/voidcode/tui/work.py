"""One app-owned work group: bounded named operations plus tracked lifecycle threads.

Every TUI background operation goes through ``submit``; long-lived loops (the
input reader and the session stream) use ``start``. Both are joined or drained
by ``shutdown`` so a quit never leaves unmanaged threads behind, and an
unexpected exception is logged with its task name instead of disappearing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

logger = logging.getLogger(__name__)

__all__ = ["WorkGroup"]


class WorkGroup:
    """Bounded executor plus tracked daemon lifecycle threads."""

    def __init__(self, *, max_workers: int = 6, thread_prefix: str = "VoidCodeTui") -> None:
        self._prefix = thread_prefix
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_prefix)
        self._names: dict[Future[None], str] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, name: str, target: Callable[[], None]) -> Future[None] | None:
        """Queue one named operation; returns None after shutdown started."""
        with self._lock:
            if self._closed:
                return None
            future = self._executor.submit(target)
            self._names[future] = name
        future.add_done_callback(self._settle)
        return future

    def _settle(self, future: Future[None]) -> None:
        with self._lock:
            name = self._names.pop(future, "unknown")
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            logger.error(
                "TUI background task %s failed",
                name,
                exc_info=(type(error), error, error.__traceback__),
            )

    @property
    def pending(self) -> int:
        """Queued or running operations (lifecycle threads are tracked separately)."""
        with self._lock:
            return len(self._names)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until every submitted operation settled; False on timeout/shutdown."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.pending:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def start(self, name: str, target: Callable[[], None]) -> threading.Thread | None:
        """Start one tracked lifecycle thread (input reader, session stream)."""
        with self._lock:
            if self._closed:
                return None
            thread = threading.Thread(target=target, name=f"{self._prefix}-{name}", daemon=True)
            self._threads.append(thread)
        thread.start()
        return thread

    def shutdown(self, *, join_timeout: float = 1.0) -> None:
        """Refuse new work, cancel queued operations, and join lifecycle threads."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = self._threads
            self._threads = []
        self._executor.shutdown(wait=False, cancel_futures=True)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(timeout=join_timeout)
