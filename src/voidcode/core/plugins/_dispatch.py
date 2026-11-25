"""Direct synchronous dispatch to tools, commands, providers, views, queries, and resource readers."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jsonschema

from voidcode.core.retry import run_with_retry
from voidcode.sdk.commands import CommandInput, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StreamableTurnProvider,
)
from voidcode.sdk.resources import ResourceContractError, ResourcePage, ResourceReadCaller, ResourceReadRequest, ResourceUnavailable
from voidcode.sdk.services import (
    QueryUnavailable,
)
from voidcode.sdk.suspend import SuspendPark
from voidcode.sdk.tools import (
    ConfirmedStop,
    TextOutput,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    is_read_tier,
)
from voidcode.sdk.views import PluginViewDescriptor

from ..services import ResourceReaderServicesView, RuntimeServices
from ._base import MAX_QUERY_RESULT_BYTES
from ._contracts import CommandInvocationOutcome, FrozenStaged, ToolInvocationOutcome, ToolSuspension, _Staged

if TYPE_CHECKING:
    from . import PluginRuntime
from ._leases import PluginLeases


class PluginDispatch(PluginLeases):
    """Direct synchronous dispatch to tools, commands, providers, views, queries, and resource readers."""

    def execute_query(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        session_id: str,
        workspace: Path,
    ) -> Mapping[str, Any] | None:
        """Run one named plugin query in this runtime; no core authority is involved."""
        entry = self._queries.get(name)
        if entry is None:
            raise QueryUnavailable(f"no admitted plugin answers the query {name!r}")
        handler, _owner = entry
        result = handler(dict(arguments), PluginQueryCaller(session_id=session_id, workspace=workspace))
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise ValueError(f"query {name!r} must answer with a JSON object or None")
        owned = own_json_object(result)
        if len(json.dumps(json_wire_object(owned), allow_nan=False)) > MAX_QUERY_RESULT_BYTES:
            raise ValueError(f"query {name!r} answer exceeded {MAX_QUERY_RESULT_BYTES} bytes")
        return owned

    def read_resource(
        self: PluginRuntime,
        request: ResourceReadRequest,
        *,
        context: ToolContext,
    ) -> ResourcePage | None:
        """Dispatch one ``scheme://`` URI to its registering provider's reader.

        Opens a provider-owner sub-lease (caller's session/run/revision,
        read-only, no internal work) and revokes it before returning. Returns
        ``None`` when the provider knows no such resource; unknown schemes
        raise ``ResourceUnavailable`` so callers keep unknown-scheme distinct
        from unknown-resource. Malformed pages raise ``ResourceContractError``.
        """
        scheme = request.scheme
        entry = self._resource_readers.get(scheme)
        if entry is None:
            raise ResourceUnavailable(f"no admitted plugin reads the resource scheme {scheme!r}")
        handler, owner = entry
        lease = self._open_lease(
            owner=owner,
            session_id=context.session_id,
            run_id=context.run_id,
            invocation_id=context.invocation_id,
            readonly=True,
            actor=context.actor,
            internal_work=False,
            invocation_depth=context.invocation_depth,
        )
        try:
            services = ResourceReaderServicesView(self, lease)
            caller = ResourceReadCaller(
                session_id=context.session_id,
                workspace=context.workspace,
                revision_id=context.revision_id,
                run_id=context.run_id,
                abort_signal=context.abort_signal,
            )
            page = handler(request, caller, services)
        finally:
            self._revoke_lease(lease)
        if page is None:
            return None
        if not isinstance(page, ResourcePage):
            raise ResourceContractError(f"resource reader {scheme!r} must answer with a ResourcePage or None")
        return page

    def invoke(
        self: PluginRuntime,
        call: ToolCall,
        *,
        context: ToolContext,
        return_outcome: bool = False,
        readonly: bool = False,
    ) -> ToolResult | ToolInvocationOutcome:
        """Invoke a registered tool by direct call."""
        if context.workspace.resolve() != self.workspace.resolve() or context.revision_id != self.revision_id:
            raise ValueError(
                f"Context mismatch: expected workspace {self.workspace} revision {self.revision_id}, "
                f"got {context.workspace} revision {context.revision_id}"
            )
        tool_entry = self._tools.get(call.tool_name)
        if tool_entry is None:
            raise ValueError(f"Unknown tool: {call.tool_name}")
        tool_instance, tool_owner = tool_entry
        tool_definition = self._tool_map.get(call.tool_name)
        if tool_definition is None:
            raise ValueError(f"Unknown tool: {call.tool_name}")
        validator = self._validators.get(call.tool_name)
        if validator is not None:
            try:
                validator.validate(json_wire_object(call.arguments))
            except jsonschema.ValidationError as val_err:
                return ToolFailure(
                    tool_name=call.tool_name,
                    error=f"Input validation error: {val_err.message}",
                    output=TextOutput(text=f"Validation error: {val_err.message}"),
                    execution=ConfirmedStop(cancellation_signalled=False),
                )

        # A tool lease is restricted when the session's effective stance is restrictive, even if
        # the tool's own effects are read-tier or spawn-tier. Internal SESSION staging stays
        # available under a read-only stance: it mutates core-owned session data, not the world.
        is_ro = readonly or (is_read_tier(tool_definition.effects) and ToolEffect.EXECUTE not in tool_definition.effects)
        lease = self._open_lease(
            owner=tool_owner,
            session_id=context.session_id,
            run_id=context.run_id,
            invocation_id=context.invocation_id,
            readonly=is_ro,
            actor=context.actor,
            internal_work=(not readonly) or ToolEffect.SESSION in tool_definition.effects,
            invocation_depth=context.invocation_depth,
            tool_callback=True,
            root_action_id=context.root_action_id,
            initial_root_budget=context.initial_root_budget,
            origin_visible_tools=context.origin_visible_tools,
            origin_readonly=context.origin_readonly,
            background_action_id=context.background_action_id,
            abort_signal=context.abort_signal,
        )
        services = RuntimeServices(self, lease)
        cancel_event = self.cancel_event_for(lease)
        progress = self.progress_callback
        if context.emit_tool_progress is None and progress is not None:
            context = replace(context, emit_tool_progress=lambda payload: progress(lease, own_json_object(payload)))
        tool_threw = False
        try:
            result = tool_instance.invoke(call, context=context, services=services)
            if not isinstance(result, (ToolSuccess, ToolFailure)):
                raise TypeError(f"Tool {call.tool_name} returned an invalid ToolResult")
            if lease.staging.suspend is not None:
                raise TypeError(
                    f"Tool {call.tool_name} staged a suspension but returned normally (SuspendPark was swallowed); staged intent discarded"
                )
        except SuspendPark as park:
            # Suspend-by-unwind: the callback raised to park itself. This clause is
            # kind-agnostic (ask now; composition reuses it later): it freezes staged
            # work exactly like the return path, revokes the lease in every sub-case,
            # and re-raises a Core-internal wrapper — the boundary never writes the
            # store; the engine's park-catch performs the atomic park commit. Clause
            # order vs except Exception is moot: SuspendPark is a BaseException.
            suspended = cancel_event.is_set() or bool(context.abort_signal and context.abort_signal.cancelled)
            if suspended:
                # Cancelled: mirror the return path — discard everything including the
                # intent, stop claimed services, revoke, and return the cancelled shape
                # so the engine settles the run with no new handling. This read is
                # best-effort; the engine park-commit re-checks the abort.
                self._cancel_claimed_services(lease)
                self._revoke_lease(lease)
                cancelled_result = ToolFailure(
                    tool_name=call.tool_name,
                    error=f"Tool {call.tool_name} of {tool_owner} was cancelled",
                    output=TextOutput(text=f"Tool {call.tool_name} was cancelled"),
                    execution=ConfirmedStop(cancellation_signalled=True),
                )
                if return_outcome:
                    return ToolInvocationOutcome(result=cancelled_result)
                return cancelled_result
            (
                staged_state,
                staged_events,
                staged_conts,
                staged_cancels,
                staged_receipts,
                staged_budget,
                staged_wakeups,
                staged_wakeup_cancels,
            ) = self._staged_changes(lease)
            frozen = FrozenStaged(
                staged_state=staged_state,
                staged_events=staged_events,
                staged_continuations=staged_conts,
                staged_cancellations=staged_cancels,
                staged_observer_receipts=staged_receipts,
                staged_budget=staged_budget,
                staged_wakeups=staged_wakeups,
                staged_wakeup_cancels=staged_wakeup_cancels,
                staged_observations=tuple(lease.staging.observations),
            )
            self._revoke_lease(lease)
            raise ToolSuspension(park, frozen) from park
        except Exception as exc:
            tool_threw = True
            result = ToolFailure(
                tool_name=call.tool_name,
                error=f"Tool {call.tool_name} of {tool_owner} failed: {exc}",
                output=TextOutput(text=f"Tool {call.tool_name} failed: {exc}"),
                execution=ConfirmedStop(cancellation_signalled=False),
            )
        cancelled = cancel_event.is_set() or bool(context.abort_signal and context.abort_signal.cancelled)
        if cancelled:
            # Cooperative cancellation discards everything this invocation staged; claimed
            # service trees were already stopped by the cancel mirror. A tool-returned stop
            # observation is the exception: the engine's stop-commit path reports the result
            # before terminating, so this invocation's staged work is preserved with it.
            self._cancel_claimed_services(lease)
            if not tool_threw and isinstance(result, ToolFailure) and result.execution is not None:
                (
                    staged_state,
                    staged_events,
                    staged_conts,
                    staged_cancels,
                    staged_receipts,
                    staged_budget,
                    staged_wakeups,
                    staged_wakeup_cancels,
                ) = self._staged_changes(lease)
                staged = ToolInvocationOutcome(
                    result=result,
                    staged_state=staged_state,
                    staged_events=staged_events,
                    staged_continuations=staged_conts,
                    staged_cancellations=staged_cancels,
                    staged_observer_receipts=staged_receipts,
                    staged_budget=staged_budget,
                    staged_wakeups=staged_wakeups,
                    staged_wakeup_cancels=staged_wakeup_cancels,
                    staged_observations=tuple(lease.staging.observations),
                )
            else:
                staged = ToolInvocationOutcome(result=result)
        elif tool_threw:
            # A thrown tool keeps no staged state — but supervisor observations are committed
            # truth about the world, not tool claims, so the prefix survives the throw.
            staged = ToolInvocationOutcome(result=result, staged_observations=tuple(lease.staging.observations))
        else:
            (
                staged_state,
                staged_events,
                staged_conts,
                staged_cancels,
                staged_receipts,
                staged_budget,
                staged_wakeups,
                staged_wakeup_cancels,
            ) = self._staged_changes(lease)
            staged = ToolInvocationOutcome(
                result=result,
                staged_state=staged_state,
                staged_events=staged_events,
                staged_continuations=staged_conts,
                staged_cancellations=staged_cancels,
                staged_observer_receipts=staged_receipts,
                staged_budget=staged_budget,
                staged_wakeups=staged_wakeups,
                staged_wakeup_cancels=staged_wakeup_cancels,
                staged_observations=tuple(lease.staging.observations),
            )
        self._revoke_lease(lease)
        if return_outcome:
            return staged
        return result

    def propose_turn(self, request: ProviderTurnRequest, *, provider: str) -> ProviderTurnResult:
        """Request a model turn, retrying adapter-approved transient failures in this runtime."""
        provider_instance = self._provider_instance(request, provider=provider)
        model_name = request.model_name or "unknown"

        def _call(attempt_number: int) -> ProviderTurnResult:
            numbered = replace(request, attempt=attempt_number)
            try:
                return provider_instance.propose_turn(numbered)
            except ProviderExecutionError:
                raise
            except Exception as exc:
                raise ProviderExecutionError(
                    kind="provider_error",
                    provider_name=provider,
                    model_name=model_name,
                    message=f"Provider {provider} turn failed: {exc}",
                ) from exc

        return run_with_retry(_call, request.abort_signal, provider=provider, model=model_name)

    def stream_turn(self, request: ProviderTurnRequest, *, provider: str) -> Iterator[ProviderStreamEvent]:
        """Stream one model turn as adapter events, without runtime retry.

        A stream is single-attempt by contract: once bytes flowed there is no
        safe resend, and a pre-first-byte failure is the caller's to re-drive
        via propose_turn, never an implicit second stream.
        """
        provider_instance = self._provider_instance(request, provider=provider)
        if not isinstance(provider_instance, StreamableTurnProvider):
            raise ProviderExecutionError(
                kind="unsupported_feature",
                provider_name=provider,
                model_name=request.model_name or "unknown",
                message=f"Provider '{provider}' does not support streaming",
                retryable=False,
            )
        try:
            yield from provider_instance.stream_turn(request)
        except ProviderExecutionError:
            raise
        except Exception as exc:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider,
                model_name=request.model_name or "unknown",
                message=f"Provider {provider} stream failed: {exc}",
            ) from exc

    def _provider_instance(self, request: ProviderTurnRequest, *, provider: str) -> Any:
        prov_entry = self._providers.get(provider)
        if prov_entry is None:
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name=provider,
                model_name=request.model_name or "unknown",
                message=f"Provider '{provider}' not configured",
            )
        provider_instance, _provider_id = prov_entry
        return provider_instance

    def invoke_command(
        self: PluginRuntime,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        context: ToolContext,
        readonly: bool = False,
    ) -> CommandInvocationOutcome:
        """Invoke a registered command by direct call."""
        cmd_entry = self._commands.get(name)
        if cmd_entry is None:
            raise ValueError(f"Unknown command: {name}")
        cmd_instance, cmd_owner = cmd_entry
        cmd_definition = cmd_instance.definition
        args = own_json_object(arguments)

        cmd_validator = self._command_validators.get(name)
        if cmd_validator is not None:
            try:
                cmd_validator.validate(json_wire_object(args))
            except jsonschema.ValidationError as val_err:
                return CommandInvocationOutcome(CommandResult(status="error", error=f"Input validation error: {val_err.message}"))

        lease = self._open_lease(
            owner=cmd_owner,
            session_id=session_id,
            run_id=context.run_id,
            invocation_id=context.invocation_id,
            readonly=readonly or (is_read_tier(frozenset(cmd_definition.effects)) and ToolEffect.EXECUTE not in cmd_definition.effects),
            actor=context.actor,
            internal_work=(not readonly) or ToolEffect.SESSION in cmd_definition.effects,
            root_action_id=context.root_action_id,
            initial_root_budget=context.initial_root_budget,
            origin_visible_tools=context.origin_visible_tools,
            origin_readonly=context.origin_readonly,
            background_action_id=context.background_action_id,
            abort_signal=context.abort_signal,
        )
        services = RuntimeServices(self, lease)
        invocation = CommandInvocation(context=context, name=name, arguments=args)
        cancel_event = self.cancel_event_for(lease)
        cmd_threw = False
        try:
            result = cmd_instance.invoke(invocation, services)
        except SuspendPark as park:
            # Commands have no run cursor to park: a suspension here (only reachable
            # by raising SuspendPark directly, since the suspend services refuse
            # non-tool leases first) is refused visibly with nothing staged. The
            # revoke is load-bearing: without it the lease — including its abort-signal
            # subscription — would leak past the invocation it was opened for.
            cmd_threw = True
            result = CommandResult(
                status="error",
                error=f"Command {name} of {cmd_owner} cannot suspend for {park.park_kind}: commands have no run cursor to park",
            )
        except Exception as exc:
            cmd_threw = True
            result = CommandResult(status="error", error=f"Command {name} of {cmd_owner} failed: {exc}")
        cancelled = cancel_event.is_set() or bool(context.abort_signal and context.abort_signal.cancelled)
        if cancelled:
            self._cancel_claimed_services(lease)
            # A cancelled invocation's partial result is not a settled output by default:
            # keep only the cancellation fact, never a paid/partial body in the receipt.
            # A command that opts in may return its own explicit cancelled error and
            # bounded partial body, which is then committed like any other result.
            preserve_cancelled = cmd_definition.preserve_cancelled_result and not cmd_threw and result.status == "error"
            if preserve_cancelled:
                changes = self._staged_changes(lease)
                # Opt-in covers the cancelled result and its audit event only; a cancelled
                # command cannot commit state, continuations, budgets, or cancellations.
                staged = ((), changes[1], (), (), (), None, (), ())
            else:
                result = CommandResult(
                    status="error",
                    error="command invocation was cancelled; external effects may be incomplete",
                    data={"cancelled": True},
                )
                staged: _Staged = ((), (), (), (), (), None, (), ())
        elif cmd_threw:
            staged = ((), (), (), (), (), None, (), ())
        else:
            staged = self._staged_changes(lease)
        self._revoke_lease(lease)
        return CommandInvocationOutcome(result, *staged, staged_observations=tuple(lease.staging.observations), cancelled=cancelled)

    def prepare_command_input(
        self: PluginRuntime, name: str, *, context: ToolContext, arguments: Mapping[str, object] | None = None
    ) -> CommandPresentation:
        """Prepare input without invoking the command or granting mutation services."""
        entry = self._commands.get(name)
        if entry is None:
            raise ValueError(f"Command {name} has no interactive input")
        command, owner = entry
        if not isinstance(command, CommandInput):
            raise ValueError(f"Command {name} has no interactive input")
        args = own_json_object(arguments or {})
        prep_validator = self._command_prep_validators.get(name)
        if prep_validator is not None:
            try:
                prep_validator.validate(json_wire_object(args))
            except jsonschema.ValidationError as val_err:
                raise ValueError(f"Command input for {name} failed schema: {val_err.message}") from val_err
        lease = self._open_lease(
            owner=owner, session_id=context.session_id, run_id=context.run_id, invocation_id=context.invocation_id, readonly=True, actor="user"
        )
        try:
            presentation = command.prepare_input(CommandInvocation(context=context, name=name, arguments=args), RuntimeServices(self, lease))
            if not isinstance(presentation, CommandPresentation):
                raise ValueError(f"Command {name} returned malformed interactive input")
            if presentation.view_type in ("choices", "form") and presentation.data.get("action") != name:
                raise ValueError(f"Command {name} prepared input for a different command")
            return presentation
        finally:
            self._revoke_lease(lease)

    def render_view(
        self: PluginRuntime,
        session_id: str,
        name: str,
        *,
        run_id: str = "",
    ) -> PluginViewDescriptor:
        """Render a declarative plugin view for a session by direct call."""
        view_entry = self._views.get(name)
        if view_entry is None:
            raise ValueError(f"Unknown view: {name}")
        view_instance, view_owner = view_entry
        lease = self._open_lease(
            owner=view_owner,
            session_id=session_id,
            run_id=run_id,
            invocation_id=uuid4().hex,
            readonly=True,
            actor="plugin",
        )
        services = RuntimeServices(self, lease)
        try:
            return view_instance.render(session_id, services)
        except Exception as exc:
            raise RuntimeError(f"View {name} of {view_owner} failed: {exc}") from exc
        finally:
            self._revoke_lease(lease)
