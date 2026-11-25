"""Lifecycle hooks, tool gates, context contributions, and generation retirement."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import uuid4

from voidcode.sdk.hooks import (
    ContextContribution,
    RunLifecycle,
    ToolIntent,
    ToolOutcome,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import (
    BackgroundActionStatus,
    ContextContributionFailed,
)

from ..services import RuntimeServices
from ..storage.store import (
    ContinuationRecord,
    ContinuationStatus,
)
from ._facts import PluginFacts

if TYPE_CHECKING:
    from . import PluginRuntime


class PluginLifecycle(PluginFacts):
    """Lifecycle hooks, tool gates, context contributions, and generation retirement."""

    def notify_background_action(self) -> None:
        """Wake the background driver loop; the default no-op keeps callback-free runtimes working."""
        callback = self.background_action_notify_callback
        if callback is not None:
            callback()

    def background_action_status(self, record: ContinuationRecord) -> BackgroundActionStatus:
        """Project one background action row to its owner-visible status; the core sets liveness.

        `interrupted` is reported when the row is claimed but its claim identity no longer
        verifies live. The verification callback is core-owned; without it a claimed row
        reports `interrupted`.
        """
        error_value = record.payload.get("error")
        error = error_value if isinstance(error_value, str) and error_value else None
        claimed = record.status
        if claimed in (ContinuationStatus.COMPLETED, ContinuationStatus.FAILED, ContinuationStatus.CANCELLED):
            return BackgroundActionStatus(action_id=record.continuation_id, status=claimed.value, error=error)
        if claimed is ContinuationStatus.PENDING:
            return BackgroundActionStatus(action_id=record.continuation_id, status=ContinuationStatus.PENDING.value)
        live = False
        verify = self.background_claim_live_callback
        if verify is not None:
            try:
                live = bool(verify(record))
            except Exception:
                live = False
        return BackgroundActionStatus(
            action_id=record.continuation_id,
            status="running" if live else "interrupted",
            error=error,
        )

    def run_lifecycle(
        self: PluginRuntime,
        owner: str,
        kind: Literal["session_start", "session_end"],
        *,
        session_id: str,
        run_id: str,
        logical_turn: str = "",
        status: Literal["completed", "failed", "interrupted", ""] = "",
        error: str | None = None,
        readonly: bool = True,
    ) -> str | None:
        """Notify one owner's lifecycle hook; returns its note (never a decision)."""
        entry = self._lifecycle_hooks.get(f"{owner}\0{kind}")
        if entry is None:
            return None
        callback, _pid = entry
        lifecycle = RunLifecycle(
            phase="start" if kind == "session_start" else "end",
            session_id=session_id,
            run_id=run_id,
            logical_turn=logical_turn,
            status=status,
            error=error if isinstance(error, str) and error else None,
        )
        lease = self._open_lease(
            owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=readonly, actor="plugin", hook=True
        )
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=lease.identity.invocation_id,
            revision_id=self.revision_id,
            actor="plugin",
            logical_turn=logical_turn,
        )
        services = RuntimeServices(self, lease)
        try:
            note = callback(lifecycle, context, services)
        except Exception as exc:
            raise RuntimeError(f"Lifecycle hook failed: {type(exc).__name__}: {exc}") from exc
        finally:
            self._revoke_lease(lease)
        if note is not None and not isinstance(note, str):
            raise ValueError(f"{kind} hook of {owner} must return str or None")
        return note

    def pre_tool(
        self: PluginRuntime,
        owner: str,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments: Mapping[str, object],
        readonly: bool,
        logical_turn: str = "",
    ) -> str | None:
        """Ask one owner's pre-tool gate whether this call may run.

        Returns a denial reason, or None to allow. The gate observes the intent: it cannot
        rewrite the call, and a failure on this path is the caller's to treat as a refusal.
        """
        lease = self._open_lease(
            owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=readonly, actor="plugin", hook=True
        )
        intent = ToolIntent(tool_name=tool_name, tool_call_id=tool_call_id, arguments=json_wire_object(arguments))
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=lease.identity.invocation_id,
            revision_id=self.revision_id,
            actor="plugin",
            logical_turn=logical_turn,
        )
        services = RuntimeServices(self, lease)
        try:
            for callback, _, _, plugin_id in self._pre_tool_hooks:
                if plugin_id != owner:
                    continue
                try:
                    reason = callback(intent, context, services)
                except Exception as exc:
                    raise RuntimeError(f"Gate failed: {type(exc).__name__}: {exc}") from exc
                if reason is not None:
                    if not isinstance(reason, str) or not reason.strip():
                        raise ValueError(f"pre_tool gate of {plugin_id} must return a non-empty reason or None")
                    return reason
            return None
        finally:
            self._revoke_lease(lease)

    def post_tool(
        self: PluginRuntime,
        owner: str,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        status: Literal["ok", "error", "cancelled"],
        text: str,
        arguments: Mapping[str, object],
        readonly: bool,
        logical_turn: str = "",
    ) -> str | None:
        """Run one owner's post-tool hook inside the governed invocation.

        The hook observes a settled result under its own lease: it cannot start nested
        governed work, and its failure is the caller's to record, never a rewrite of the
        tool verdict this invocation already decided.
        """
        lease = self._open_lease(
            owner=owner, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=readonly, actor="plugin", hook=True
        )
        outcome = ToolOutcome(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status=status,
            text=text,
            arguments=json_wire_object(arguments),
        )
        context = ToolContext(
            workspace=self.workspace,
            session_id=session_id,
            run_id=run_id,
            invocation_id=lease.identity.invocation_id,
            revision_id=self.revision_id,
            actor="plugin",
            logical_turn=logical_turn,
        )
        services = RuntimeServices(self, lease)
        try:
            note: str | None = None
            for callback, _, _, plugin_id in self._post_tool_hooks:
                if plugin_id != owner:
                    continue
                try:
                    returned = callback(outcome, context, services)
                except Exception as exc:
                    raise RuntimeError(f"Hook failed: {type(exc).__name__}: {exc}") from exc
                if returned is not None:
                    if not isinstance(returned, str):
                        raise ValueError(f"post_tool hook of {plugin_id} must return str or None")
                    note = returned
            return note
        finally:
            self._revoke_lease(lease)

    def contribute_context(
        self: PluginRuntime,
        session_id: str,
        *,
        run_id: str = "",
        logical_turn: str = "",
    ) -> tuple[dict[str, object], ...]:
        contributions: list[dict[str, object]] = []
        for plugin_id, callback in self._context_callbacks.items():
            lease = self._open_lease(owner=plugin_id, session_id=session_id, run_id=run_id, invocation_id=uuid4().hex, readonly=True, actor="plugin")
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=run_id,
                invocation_id=lease.identity.invocation_id,
                revision_id=self.revision_id,
                actor="plugin",
                logical_turn=logical_turn,
            )
            services = RuntimeServices(self, lease)
            try:
                contribution = callback(context, services)
            except Exception as exc:
                raise ContextContributionFailed(f"Context provider {plugin_id} failed: {type(exc).__name__}: {exc}") from exc
            finally:
                self._revoke_lease(lease)
            if isinstance(contribution, ContextContribution):
                contributions.append({"type": "text", "content": contribution.content, "owner": plugin_id, "role": contribution.role})
        return tuple(contributions)

    def _forget_module(self, name: str) -> None:
        sys.modules.pop(name, None)

    def close(self) -> None:
        """Retire this generation: stop its services, run close callbacks, unimport its modules."""
        if self._closed:
            return
        self._closed = True
        with self._lease_lock:
            for lease in self._active_leases.values():
                lease.revoked = True
            self._active_leases.clear()
            self._claimed_services.clear()
            self._cancel_events.clear()
        # Child and service processes started by this generation stop with it. Detached
        # background jobs live in the core-owned ProcessHost and survive a generation swap;
        # Core.close() is the owner shutdown that stops them.
        try:
            self.processes.reap_all()
        finally:
            self.processes.stop_services()
        # Run close callbacks for loaded plugins, propagating first error after unimport.
        # A hung callback blocks retire until core restart — there is no kill isolation.
        first_error: BaseException | None = None
        for plugin in self._plugins:
            if plugin.close is not None and callable(plugin.close):
                try:
                    plugin.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        # Unimport generation namespaces so reloaded generations avoid stale modules.
        try:
            for name in self._modules:
                self._forget_module(name)
            self._modules.clear()
        finally:
            if first_error is not None:
                raise first_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
