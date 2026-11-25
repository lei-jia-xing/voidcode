"""Owner-scheduled wakeup claiming, driving, and the core-owned ticker.

A due wakeup is claimed durably, delivered as a fact, driven as a governed run
under its session lock, and settled at the run terminal."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from typing import Literal
from uuid import uuid4

from ..cancellation import Cancellation
from ..events import EventType
from ..execution import _Cursor, _Execution, committed_event
from ..storage.store import SessionRecord, SessionStatus, StoreConflict, WakeupRecord, WakeupSkip
from ._lifecycle import CoreLifecycle
from .turn_execution import CoreResult

logger = logging.getLogger(__name__)

#: Bound asynchronous wakeup workers so a due backlog cannot create one thread per row.
#: Explicit `tick()` calls remain synchronous and are not subject to this budget.
MAX_ACTIVE_WAKEUP_RUNS = 8


class CoreWakeups(CoreLifecycle):
    """Owner-scheduled wakeup claiming, driving, and the core-owned ticker."""

    def wakeup_tick_status(self) -> dict[str, object]:
        """Last wakeup tick's bounded claim/skip/error report (never a delivery log)."""
        with self._wakeup_tick_lock:
            state = dict(self._wakeup_tick_state)
        claimed_val = state.get("claimed")
        skipped_val = state.get("skipped")
        return {
            "at_ms": state.get("at_ms"),
            "claimed": tuple(claimed_val) if isinstance(claimed_val, Sequence) else (),
            "skipped": tuple(skipped_val) if isinstance(skipped_val, Sequence) else (),
            "error": state.get("error"),
        }

    def _record_wakeup_tick(
        self,
        *,
        claimed: Sequence[str],
        skipped: Sequence[dict[str, str]],
        error: str | None,
    ) -> None:
        with self._wakeup_tick_lock:
            self._wakeup_tick_state = {
                "at_ms": int(time.time() * 1000),
                "claimed": tuple(claimed)[:32],
                "skipped": tuple(skipped)[:32],
                "error": None if error is None else error[:500],
            }

    @staticmethod
    def _skip_payload(skipped: Sequence[WakeupSkip]) -> list[dict[str, str]]:
        return [{"wakeup_id": item.wakeup_id, "session_id": item.session_id, "reason": item.reason} for item in skipped[:32]]

    def tick(self, now_ms: int | None = None) -> dict[str, object]:
        """Claim every currently due wakeup and drive the ones whose sessions can start.

        Explicit calls run each claimed turn synchronously. The core's own ticker
        marks its thread-local dispatch mode and launches each claimed turn on its
        own worker under that session's lock, so a long run never blocks the ticker
        or another due session.
        """
        self.ensure_open()
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if type(now) is not int or now < 0:
            raise ValueError("now_ms must be a non-negative integer")
        async_dispatch = bool(getattr(self._wakeup_tick_mode, "async_dispatch", False))
        claim_limit: int | None = None
        if async_dispatch:
            with self._session_locks_guard:
                self._wakeup_workers = [worker for worker in self._wakeup_workers if worker.is_alive()]
                active_workers = len(self._wakeup_workers)
            claim_limit = MAX_ACTIVE_WAKEUP_RUNS - active_workers
            if claim_limit <= 0:
                self._record_wakeup_tick(claimed=(), skipped=(), error=None)
                return {"fired": None}
        claims, skipped = self.store.claim_due_wakeups(
            str(self.workspace),
            now,
            revision_id=self.revision_id,
            eligible_owners=self.plugin_runtime.observer_owners.get("wakeup_fired", ()),
            limit=claim_limit,
        )
        skipped_payload = self._skip_payload(skipped)
        self._record_wakeup_tick(claimed=[wakeup.wakeup_id for wakeup, _session in claims], skipped=skipped_payload, error=None)
        if not claims:
            return {"fired": None, **({"skipped": skipped_payload} if skipped_payload else {})}
        self.notify_commit()
        if async_dispatch:
            for wakeup, session in claims:
                self._launch_claimed_wakeup(wakeup, session)
            first_wakeup, first_session = claims[0]
            return {
                "fired": first_wakeup.wakeup_id,
                "fired_ids": [wakeup.wakeup_id for wakeup, _session in claims],
                "session_id": first_session.session_id,
                "status": "dispatched",
                "wakeup_status": "claimed",
                **({"skipped": skipped_payload} if skipped_payload else {}),
            }
        results = [self._drive_claimed_wakeup(wakeup, session) for wakeup, session in claims]
        first = results[0]
        first["fired_ids"] = [wakeup.wakeup_id for wakeup, _session in claims]
        if len(results) > 1:
            first["results"] = results
        if skipped_payload:
            first["skipped"] = skipped_payload
        return first

    def _launch_claimed_wakeup(self, wakeup: WakeupRecord, session: SessionRecord) -> None:
        """Run one already-claimed wakeup turn on its own worker."""
        worker = threading.Thread(
            target=self._drive_claimed_wakeup_safely,
            args=(wakeup, session),
            name=f"CoreWakeup:{session.session_id[:16]}",
            daemon=True,
        )
        with self._session_locks_guard:
            live = [item for item in self._wakeup_workers if item.is_alive()]
            live.append(worker)
            self._wakeup_workers = live
        worker.start()

    def _drive_claimed_wakeup_safely(self, wakeup: WakeupRecord, session: SessionRecord) -> None:
        try:
            self._drive_claimed_wakeup(wakeup, session)
        except Exception as exc:
            message = f"wakeup {wakeup.wakeup_id} run failed: {type(exc).__name__}: {exc}"
            try:
                self.store.abandon_claimed_wakeup(
                    wakeup.wakeup_id,
                    reason=message,
                    eligible_owners=self.plugin_runtime.observer_owners.get("run_finished", ()),
                )
            except Exception as recovery_exc:
                logger.error("wakeup %s claim cleanup failed: %s: %s", wakeup.wakeup_id, type(recovery_exc).__name__, recovery_exc)
            # The newly committed terminal event still needs its fact delivery; leaving it
            # only for startup replay would delay the owner's settlement indefinitely.
            try:
                self._fact_delivery.replay_pending()
            except Exception as delivery_exc:
                logger.warning("wakeup %s finish replay failed: %s: %s", wakeup.wakeup_id, type(delivery_exc).__name__, delivery_exc)
            self._record_wakeup_tick(claimed=(), skipped=(), error=message)
            logger.warning("%s", message)

    def _drive_claimed_wakeup(self, wakeup: WakeupRecord, session: SessionRecord) -> dict[str, object]:
        """Drive one claimed wakeup under its session lock and report its terminal status."""
        wakeup_id = wakeup.wakeup_id
        root_id = wakeup.root_id
        run_id = wakeup.claim_run_id or session.run_id or ""
        wakeup_sequence = session.last_event_sequence
        logical_turn = uuid4().hex
        abort = Cancellation()
        self.register_run(session.session_id, run_id, abort)
        try:
            with self._session_lock(session.session_id):
                self.ensure_open()
                # Commands may commit beside this run without a session run lock. Re-read
                # the tip under the lock so the driver's in-memory prefix matches durable
                # order; the claim event itself stays addressed by its recorded sequence.
                current = self.store.session(session.session_id)
                if current.run_id != run_id:
                    raise StoreConflict(f"wakeup {wakeup_id!r} claim was superseded by run {current.run_id!r}")
                cursor = _Cursor(root_id=root_id, logical_turn=logical_turn)
                execution = _Execution(current, cursor, list(self.store.events(session.session_id)), run_id)
                self._fact_delivery.deliver(
                    execution,
                    (committed_event(execution, wakeup_sequence, EventType.WAKEUP_FIRED),),
                )
                result = self._turn_driver.drive(execution)
        finally:
            self.unregister_run(session.session_id)
        terminal = self._wakeup_terminal_status(result)
        if terminal is not None:
            # The run-finished commit already settled the claim atomically; this
            # idempotent call keeps the explicit tick contract when a driver
            # returns a terminal result without a new claim row.
            self.store.complete_wakeup(wakeup_id, terminal)
        return {
            "fired": wakeup_id,
            "session_id": session.session_id,
            "status": result.status,
            "wakeup_status": terminal if terminal is not None else "claimed",
        }

    @staticmethod
    def _wakeup_terminal_status(result: CoreResult) -> Literal["completed", "failed", "cancelled"] | None:
        """The wakeup settlement a run result implies, or None while the run is parked."""
        if result.status in ("completed", "idle"):
            return "completed"
        if result.status == "failed":
            return "failed"
        if result.status in (SessionStatus.WAITING, SessionStatus.RUNNING, "waiting", "running", "queued"):
            return None
        if result.stop_cause in ("user_stop", "owner_cancel"):
            return "cancelled"
        return "failed"

    def _wakeup_loop(self) -> None:
        """Core-owned tick: claim every due wakeup, launch each run, never replay slots."""
        self._wakeup_tick_mode.async_dispatch = True
        last_error: str | None = None
        try:
            while not self._closed:
                try:
                    outcome = self.tick()
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    self._record_wakeup_tick(claimed=(), skipped=(), error=message)
                    if message != last_error:
                        logger.warning("wakeup tick failed: %s", message)
                        last_error = message
                    time.sleep(1.0)
                    continue
                last_error = None
                if outcome.get("fired") is not None:
                    continue
                with self._session_locks_guard:
                    self._wakeup_workers = [item for item in self._wakeup_workers if item.is_alive()]
                time.sleep(1.0)
        finally:
            self._wakeup_tick_mode.async_dispatch = False
