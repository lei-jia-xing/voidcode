"""One manager's owner thread and its shutdown lifecycle."""

from __future__ import annotations

import concurrent.futures
import queue
import threading
from collections.abc import Callable
from typing import Any, Final, ParamSpec, TypeVar

_OWNER_CLOSE_JOIN_SECONDS: Final[float] = 5.0
_P = ParamSpec("_P")
_T = TypeVar("_T")


class LspOwner:
    """One manager's owner thread for LSP runtime objects.

    ``call`` runs ``func`` on that thread and returns its result (or raises
    its exception). Calls issued from the owner thread itself run inline, so
    owned code can freely use the routing entry points without self-deadlock.
    The thread starts lazily and ``close`` stops it after queued work so a
    manager owns the whole lifecycle; one manager's shutdown never affects
    another manager.
    """

    def __init__(self) -> None:
        self._work: queue.Queue[tuple[Callable[[], Any], concurrent.futures.Future[Any]] | None] = queue.Queue()
        self._ident: int | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.RLock()

    def call(self, func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        with self._lock:
            self._ensure_started_locked()
            if threading.get_ident() == self._ident:
                return func(*args, **kwargs)
            future: concurrent.futures.Future[Any] = concurrent.futures.Future()
            self._work.put((lambda: func(*args, **kwargs), future))
        return future.result()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            if thread is None:
                return
            if threading.get_ident() == self._ident:
                raise RuntimeError("the LSP owner thread cannot join itself")
            self._work.put(None)
        thread.join(timeout=_OWNER_CLOSE_JOIN_SECONDS)
        if thread.is_alive():
            raise RuntimeError("the LSP owner thread did not stop")

    def _ensure_started_locked(self) -> None:
        if self._closed:
            raise RuntimeError("the LSP manager is shut down")
        if self._thread is None:
            self._thread = threading.Thread(target=self._serve, name="lsp-owner", daemon=True)
            self._thread.start()

    def _serve(self) -> None:
        self._ident = threading.get_ident()
        while True:
            item = self._work.get()
            if item is None:
                return
            func, future = item
            try:
                future.set_result(func())
            except BaseException as exc:  # noqa: B036 - delivered to the caller; the owner itself never exits
                future.set_exception(exc)
