"""Reserved command dispatch and the typed command/control surface.

Catalog resolution, input rendering, confirmation, reserved `core.reload`, and
continuation draining stay on the facade; execution belongs to the command
runtime."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Literal
from uuid import uuid4

import jsonschema

from ...sdk.command_help import grouped_help_entries
from ...sdk.commands import CommandCatalog, CommandInputSnapshot, CommandResult
from ...sdk.invocation import ToolContext
from ...sdk.json_values import json_wire_object
from ...sdk.services import ContinuationResult
from ...sdk.views import PluginViewDescriptor, ViewsSnapshot
from ..cancellation import Cancellation
from ..storage.store import SessionRecord, SessionStatus, StagedCancellation, StaleRevision
from ._capability import _RELOAD_COMMAND_SPEC, RELOAD_COMMAND_NAME
from ._lifecycle import CoreLifecycle
from .command_runtime import CommandPlan


class CoreCommands(CoreLifecycle):
    """Reserved command dispatch and the typed command/control surface."""

    def command_catalog(self) -> CommandCatalog:
        """Command catalog captured together with the revision that admits it.

        Descriptor list and revision come from ONE immutable snapshot, so a client can
        bind a later action to exactly the generation it displayed.
        """
        self.ensure_open()
        snapshot = self._snapshot
        return CommandCatalog(revision_id=snapshot.revision_id, commands=snapshot.commands)

    def command_status(self, session: SessionRecord) -> CommandResult:
        """Reserved status command: session truth, usage, budgets, and event counts."""
        data = self.show(session.session_id, events=False)
        counts = self.store.event_type_counts(session.session_id)
        usage = self.usage(session.session_id)
        budgets = self.store.session_budgets(session.session_id)
        data["event_counts"] = counts
        data["usage"] = usage.as_payload()
        data["budgets"] = [budget.as_payload() for budget in budgets]
        model = self.config.model
        lines = [
            f"session: {session.session_id}",
            f"status: {session.status.value}",
            f"workspace: {self.workspace}",
            f"revision: {session.active_revision_id}",
            f"model: {model}" if model is not None else "model: not configured",
            f"run: {session.run_id or 'none'}",
            f"turn: {session.turn}, last event: {session.last_event_sequence}",
            f"turns: {usage.turn_count}, input tokens: {usage.input_tokens}, output tokens: {usage.output_tokens}, cost: {usage.total_cost_usd}",
        ]
        lines.extend("budget: " + ", ".join(f"{key}={value}" for key, value in budget.as_payload().items()) for budget in budgets)
        lines.extend(f"{event_type}: {count}" for event_type, count in sorted(counts.items()))
        return CommandResult(status="ok", output="\n".join(lines), data=data)

    def command_help(self) -> CommandResult:
        """Reserved help command: the attached revision's command catalog with usage."""
        catalog = self.command_catalog()
        lines = ["Available commands:"]
        for group, entries in grouped_help_entries(catalog.commands):
            lines.append("")
            lines.append(f"{group}:")
            for entry in entries:
                lines.append(f"  {'  ' * (entry.depth - 1)}{entry.usage} — {entry.description}")
                lines.extend(f"    {'  ' * (entry.depth - 1)}e.g. {example}" for example in entry.examples)
        return CommandResult(status="ok", output="\n".join(lines), data=catalog.as_payload())

    def owner_cancel_claimed_work(
        self,
        session_id: str,
        cancellations: Sequence[StagedCancellation],
        reason: str,
    ) -> None:
        """Settle work whose own claimed continuation an owner just cancelled."""
        self._continuation_scheduler.settle_cancellations(session_id, cancellations, reason=reason)

    @contextmanager
    def command_abort(self, context: ToolContext) -> Generator[ToolContext]:
        # Commands may run alongside a turn and other commands: each owns its signal.
        self.ensure_open()
        abort = Cancellation()
        self._cancellations.register_command(context.session_id, context.invocation_id, abort)
        try:
            yield replace(context, abort_signal=abort)
        finally:
            self._cancellations.unregister_command(context.session_id, context.invocation_id)

    def command_input(
        self, session_id: str, name: str, *, expected_revision: str, arguments: Mapping[str, object] | None = None
    ) -> CommandInputSnapshot:
        """Render optional plugin-owned input; never execute the command to discover values."""
        self.ensure_open()
        session = self.session(session_id)
        with self.dispatch("command_input") as snapshot:
            if expected_revision != snapshot.revision_id:
                raise StaleRevision("displayed catalog revision is stale; refresh the catalog", expected_revision, snapshot.revision_id)
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=session.run_id or "",
                invocation_id=uuid4().hex,
                revision_id=snapshot.revision_id,
                actor="user",
            )
            presentation = snapshot.plugin_runtime.prepare_command_input(name, context=context, arguments=arguments)
            return CommandInputSnapshot(revision_id=snapshot.revision_id, presentation=presentation)

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Capture one command plan and delegate its governed invocation to the command runtime.

        This entrypoint keeps catalog resolution, session truth, invocation identity, and the
        reserved reload command; the runtime owns policy checks, dispatch, confirmation staging,
        and the atomic public commit. `expected_revision` is required and must be the revision
        the client displayed: a command advertised by one generation is never silently executed
        by another.
        """
        self.ensure_open()
        self._check_expected_revision(expected_revision)
        session = self.session(session_id)

        # Core reserved commands dispatch through the advertised typed contract.
        if name == RELOAD_COMMAND_NAME:
            return self._invoke_reload_command(session_id, arguments)

        descriptor = self._snapshot.command_of(name)
        if descriptor is None:
            raise ValueError(f"Unknown command: {name}")
        read_only = self.session_read_only(session_id)
        core_command = descriptor.owner == "core"
        root_action_id: str | None = None
        initial_root_budget: int | None = None
        origin_visible_tools: tuple[str, ...] | None = None
        if not core_command:
            root_action_id, initial_root_budget = self.capture_root_authorization(session)
            origin_visible_tools = self.visible_tool_names_for_session(session_id, self._snapshot)
        plan = CommandPlan(
            descriptor=descriptor,
            arguments=arguments,
            expected_revision=expected_revision,
            effects=frozenset(descriptor.effects),
            read_only=read_only,
            session=session,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            context=ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=session.run_id or uuid4().hex,
                invocation_id=uuid4().hex,
                revision_id=self.revision_id,
                actor="client",
                logical_turn=uuid4().hex,
                root_action_id=root_action_id,
                initial_root_budget=initial_root_budget,
                origin_visible_tools=origin_visible_tools,
                origin_readonly=None if core_command else read_only,
            ),
        )
        return self._command_runtime.invoke(plan)

    def confirm_action(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        expected_revision: str,
    ) -> CommandResult:
        """Resolve a pending confirmation request by exact ID and state version."""
        self.ensure_open()
        self._check_expected_revision(expected_revision)
        return self._command_runtime.confirm(
            self.session(session_id),
            request_id,
            decision,
            expected_revision=expected_revision,
        )

    def plugin_views(
        self,
        session_id: str,
    ) -> ViewsSnapshot:
        """Render declarative views contributed by admitted plugins for the session.

        The whole set renders under ONE captured generation and carries that revision,
        so a late view from an older revision is never relabelled with the new one.
        """
        self.ensure_open()
        session = self.session(session_id)
        with self.dispatch("views") as snapshot:
            views: list[PluginViewDescriptor] = []
            for view_name in snapshot.plugin_runtime.views:
                try:
                    v = snapshot.plugin_runtime.render_view(session_id, view_name, run_id=session.run_id or "")
                    views.append(v)
                except Exception as exc:
                    owner = snapshot.plugin_runtime.view_owner(view_name)
                    views.append(
                        PluginViewDescriptor(
                            owner=owner,
                            name=view_name,
                            title=f"{view_name} (error)",
                            view_type="notice",
                            data={"message": f"View {view_name} failed: {type(exc).__name__}", "level": "error"},
                        )
                    )
            return ViewsSnapshot(revision_id=snapshot.revision_id, views=tuple(views))

    def drain_continuations(
        self,
        session_id: str,
        *,
        limit: int = 10,
        max_steps: int | None = None,
        continuation_id: str | None = None,
        _automatic: bool = False,
    ) -> tuple[ContinuationResult, ...]:
        """Claim and execute pending continuations as governed runs with plugin provenance."""
        return self._continuation_scheduler.drain(
            session_id,
            limit=limit,
            max_steps=max_steps,
            continuation_id=continuation_id,
            automatic=_automatic,
        )

    def _invoke_reload_command(self, session_id: str, arguments: Mapping[str, object]) -> CommandResult:
        """Reserved core command: activate the configured sources on explicit user request."""
        validator = jsonschema.Draft202012Validator(json_wire_object(_RELOAD_COMMAND_SPEC[2]))
        try:
            validator.validate(json_wire_object(arguments))
        except jsonschema.ValidationError as err:
            return CommandResult(status="error", error=f"Command arguments failed schema: {err.message}")
        if self._admits is None:
            return CommandResult(status="error", error="capability activation is unavailable")
        if self.config.policy.read_only:
            return CommandResult(status="error", error="read-only policy denies capability activation")
        session = self.session(session_id)
        if session.status not in {
            SessionStatus.IDLE,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.INTERRUPTED,
        }:
            return CommandResult(
                status="error",
                error=f"session {session_id} is busy ({session.status.value}); activation requires an idle session",
            )
        outcome = self.activate(session_id=session_id, source="client")
        if not outcome["activated"]:
            return CommandResult(status="error", error=str(outcome["error"]))
        return CommandResult(status="ok", data={key: value for key, value in outcome.items() if key != "error"})
