"""Follow one session: stream thread, health/reconcile rounds, invalidation tokens.

The follower owns everything that watches the core in the background: the
session event-stream thread, the health + connection-reconcile workers, the
session epoch, and the abort tokens that invalidate stale async replies. It
produces event-queue frames; :class:`CoreTuiApp` keeps consuming them.

Guard mapping (four counters collapse to one epoch plus tokens):

* ``_generation`` → :attr:`epoch`. Session/stream scope: bumped only by
  :meth:`new_epoch` on session switch. Frames carry the int; the drain drops
  foreign epochs exactly as before.
* ``_reconcile_serial`` + ``_reconcile_applied`` → one reconcile token per
  round. The two envelopes of a round (``health`` + ``reconciled``) share it;
  a new round aborts the previous token. The applied floor is subsumed: the
  only order the producer emits is health-then-reconciled (same worker,
  sequential puts, FIFO queue), and any older round is aborted, so "newest
  token wins" is exactly "newest serial wins".
* ``_interaction_serial`` → interaction token. Input-prep and presentation
  replies carry it; invoke/overlay/composer/view paths abort without
  beginning (they only invalidate pending input frames).
* ``_live_serial`` → live token. Live refresh replies carry it; close and
  suspend paths abort without beginning.

Tokens beat serials: invalidation is explicit (teardown paths abort rather
than bumping a counter and hoping), no integer applies anywhere, and a token
answers one question — "was I superseded?" — instead of encoding an order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import threading
from collections.abc import Callable

from voidcode.sdk.client import CoreClientError, ErrorFrame, EventFrame, HeadFrame, RestartFrame, SessionClient, StreamCancellation

from .events import AbortToken, HealthEvent, ReconciledEvent, ServerRestartEvent, StreamErrorEvent, StreamEventEvent, StreamHeadEvent, TuiFrame
from .work import WorkGroup

logger = logging.getLogger(__name__)


class SessionFollower:
    """Background session watcher: stream thread, health, reconcile, tokens.

    Constructed once per app with shared sinks; :meth:`new_epoch` rebinds it
    to another session. All workers are daemon threads putting dict frames on
    the shared queue; the app drains them on its own thread.
    """

    def __init__(
        self,
        *,
        client: SessionClient,
        queue: queue.Queue[TuiFrame],
        stop_event: threading.Event,
        cursors: dict[str, int],
        on_reconnect: Callable[[], None],
        work: WorkGroup,
    ) -> None:
        self._client = client
        self._queue = queue
        self._stop_event = stop_event
        self._cursors = cursors
        self._on_reconnect = on_reconnect
        self._work = work
        self._epoch = 0
        self._interaction_token = AbortToken()
        self._live_token = AbortToken()
        self._reconcile_token = AbortToken()
        self._thread: threading.Thread | None = None
        self._cancel: StreamCancellation | None = None

    @property
    def epoch(self) -> int:
        """Current session epoch; bumped only by :meth:`new_epoch`."""
        return self._epoch

    def new_epoch(self) -> int:
        """Rebind to a new session: bump the epoch and abort every pending token."""
        self._epoch += 1
        self._interaction_token.abort()
        self._interaction_token = AbortToken()
        self._live_token.abort()
        self._live_token = AbortToken()
        self._reconcile_token.abort()
        self._reconcile_token = AbortToken()
        return self._epoch

    def begin_interaction(self) -> AbortToken:
        """Open a command-input round; supersedes any pending input reply."""
        self._interaction_token.abort()
        self._interaction_token = AbortToken()
        return self._interaction_token

    def abort_interaction(self) -> None:
        """Invalidate pending input replies without opening a round."""
        self._interaction_token.abort()
        self._interaction_token = AbortToken()

    def begin_live(self) -> AbortToken:
        """Open a live-refresh round; supersedes any pending live reply."""
        self._live_token.abort()
        self._live_token = AbortToken()
        return self._live_token

    def abort_live(self) -> None:
        """Invalidate pending live replies without opening a round."""
        self._live_token.abort()
        self._live_token = AbortToken()

    def begin_reconcile(self) -> AbortToken:
        """Open a health/reconcile round; supersedes any pending round."""
        self._reconcile_token.abort()
        self._reconcile_token = AbortToken()
        return self._reconcile_token

    @property
    def thread(self) -> threading.Thread | None:
        """Current stream thread, if a stream was ever started."""
        return self._thread

    @property
    def cancel_handle(self) -> StreamCancellation | None:
        """Current generation's stream cancellation handle, if any."""
        return self._cancel

    def start_stream(self, session_id: str) -> None:
        """Follow ``session_id`` on a fresh stream thread, retiring the previous handle."""
        epoch = self._epoch
        previous, self._cancel = self._cancel, StreamCancellation()
        if previous is not None:
            previous.close()
        cancel = self._cancel

        async def stream_worker() -> None:
            first_connection = True
            while not self._stop_event.is_set() and epoch == self._epoch:
                after_seq = self._cursors.get(session_id, 0)
                if not first_connection:
                    self._on_reconnect()
                first_connection = False
                try:
                    async with contextlib.aclosing(
                        self._client.events_stream(session_id, after_sequence=after_seq, follow=True, cancel=cancel)
                    ) as stream:
                        async for frame in stream:
                            if self._stop_event.is_set() or epoch != self._epoch:
                                return
                            if isinstance(frame, HeadFrame):
                                self._queue.put(StreamHeadEvent(frame=frame, session_id=session_id, generation=epoch))
                            elif isinstance(frame, EventFrame):
                                self._queue.put(StreamEventEvent(frame=frame, session_id=session_id, generation=epoch))
                            elif isinstance(frame, RestartFrame):
                                self._queue.put(ServerRestartEvent(session_id=session_id, generation=epoch))
                                break
                            elif isinstance(frame, ErrorFrame):
                                self._queue.put(StreamErrorEvent(error=frame.error, session_id=session_id, generation=epoch))
                                return
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    if self._stop_event.is_set() or epoch != self._epoch:
                        return
                    if isinstance(exc, CoreClientError) and exc.status_code in {400, 401, 403, 404}:
                        self._queue.put(StreamErrorEvent(error=str(exc), session_id=session_id, generation=epoch))
                        return
                    logger.debug("session event stream reconnecting after: %s", exc)
                    await asyncio.sleep(1.0)

        def run_worker() -> None:
            asyncio.run(stream_worker())

        def set_thread(thread: threading.Thread) -> None:
            self._thread = thread

        self._thread = self._work.start("Stream", run_worker, on_created=set_thread)

    def refresh_health(self, session_id: str, generation: int) -> None:
        """Refresh server identity/liveness off the UI thread; stale replies are dropped."""
        token = self.begin_reconcile()

        def worker() -> None:
            try:
                snapshot = self._client.health()
            except Exception:
                return  # unreachable during a handoff gap is expected; keep last known
            self._queue.put(HealthEvent(health=snapshot, token=token, session_id=session_id, generation=generation))

        self._work.submit("Health", worker)

    def reconcile_connection(self, session_id: str) -> None:
        """ONE connection-established reconciliation path, entirely off the UI thread.

        Runs on the first frame of every (re)connected stream — announced restart
        or silent drop. Fetches health + catalog + durable session status in one
        worker round, then hands the UI thread token-tagged envelopes.
        """
        token = self.begin_reconcile()
        generation = self._epoch

        def worker() -> None:
            try:
                health = self._client.health()
            except Exception:
                return  # handoff gap: keep last known identity, stay silent
            failures: list[str] = []
            try:
                catalog = self._client.command_catalog()
            except Exception as exc:
                catalog = None
                failures.append(f"catalog ({exc})")
            try:
                session_status = self._client.get_session(session_id).status
            except Exception as exc:
                session_status = ""
                failures.append(f"session status ({exc})")
            self._queue.put(HealthEvent(health=health, token=token, session_id=session_id, generation=generation))
            self._queue.put(
                ReconciledEvent(
                    catalog=catalog,
                    session_status=session_status,
                    failures=tuple(failures),
                    token=token,
                    session_id=session_id,
                    generation=generation,
                )
            )

        self._work.submit("Reconcile", worker)
