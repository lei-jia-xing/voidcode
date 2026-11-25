"""Governed invocation services: tools, resources, model queries, and suspension."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, NoReturn
from uuid import uuid4

from voidcode.sdk.ask import AskAlreadyParked, AskRequestDraft, SuspendAsk
from voidcode.sdk.json_values import own_json_object
from voidcode.sdk.services import ArtifactReference, ModelQueryUnavailable, StructuredModelResult, ToolInvocationReceipt

from ..artifacts import SHA256_PATTERN
from ..events import EventType
from ..storage.store import CheckpointKind, SessionStatus
from ._lease import ServiceLease

if TYPE_CHECKING:
    from voidcode.sdk.foreground import CompositionDraft
    from voidcode.sdk.resources import ResourcePage, ResourceReadRequest
    from voidcode.sdk.tools import ToolDefinition

    from ..plugins import LeaseContext


class SuspendOutsideTool(PermissionError):
    """A non-tool lease attempted suspension; only tool callbacks have a run cursor to park."""


class InvocationServices(ServiceLease):
    """Tool/resource/model-query requests and their typed suspension paths."""

    def _abort_signal_for(self, lease: LeaseContext) -> _LeaseAbortSignal | _CancelledAbortSignal:
        """The caller's cancellation handle, so reader paging can stop promptly."""
        event = self._runtime.cancel_event_for(lease)
        if event.is_set():
            return _CancelledAbortSignal()
        return _LeaseAbortSignal(event)

    def tool_definition(self, name: str) -> ToolDefinition | None:
        """One definition from the active revision's dispatch snapshot, with this session's visibility.

        Answered by the core, never from a runtime-local copy: only the core knows the reserved
        core surface and the session's profile scope and read-only stance. ``None`` covers both an
        unknown name and a name this session may not see.
        """
        from voidcode.sdk.tools import ToolDefinition as Definition

        lease = self._active_lease()
        callback = self._runtime.tool_definition_callback
        if callback is None:
            raise RuntimeError("tool definitions are unavailable")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool_definition requires a non-empty name")
        definition, _reason = callback(lease.identity.session_id, name.strip(), lease.identity.revision_id)
        if definition is None:
            return None
        if isinstance(definition, Definition):
            return definition
        if not isinstance(definition, Mapping):
            raise RuntimeError("tool definitions are unavailable")
        from voidcode.sdk.codec import decode_tool_definition

        return decode_tool_definition(definition)

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        identity = self._active_lease().identity
        return self._runtime.execute_query(
            str(name), own_json_object(dict(arguments or {})), session_id=identity.session_id, workspace=identity.workspace
        )

    def read_resource(self, request: ResourceReadRequest) -> ResourcePage | None:
        from voidcode.sdk.invocation import ToolContext

        lease = self._active_lease()
        self._check_hook("read_resource")
        abort_signal = self._abort_signal_for(lease)
        context = ToolContext(
            workspace=lease.identity.workspace,
            session_id=lease.identity.session_id,
            run_id=lease.identity.run_id,
            invocation_id=lease.identity.invocation_id,
            revision_id=lease.identity.revision_id,
            actor=lease.identity.actor,
            invocation_depth=lease.identity.invocation_depth,
            abort_signal=abort_signal,
        )
        return self._runtime.read_resource(request, context=context)

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        from voidcode.sdk.codec import encode_tool_call
        from voidcode.sdk.tools import ToolCall

        from ..storage.store import EventDraft

        lease = self._active_lease()
        self._check_hook("invoke_tool")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invoke_tool requires a non-empty name")
        if expected_revision != self._runtime.revision_id:
            raise ValueError("invoke_tool expected_revision differs from the active revision")
        if not isinstance(arguments, Mapping) and arguments is not None:
            raise ValueError("invoke_tool arguments must be an object")
        if lease.identity.background_action_id is not None and (
            lease.identity.origin_visible_tools is None or lease.identity.origin_readonly is None or not lease.identity.root_action_id
        ):
            raise PermissionError("background tool requests require captured origin tool, stance, and root authorization")
        request_id = uuid4().hex
        depth = lease.identity.invocation_depth + 1
        call = ToolCall(name, own_json_object(dict(arguments or {})), tool_call_id=request_id)
        root_id = lease.identity.root_action_id or ""
        payload: dict[str, object] = {
            "request_id": request_id,
            "call": encode_tool_call(call),
            "expected_revision": expected_revision,
            "requester": lease.identity.owner,
            "root_id": root_id,
            "root_action_budget": lease.identity.initial_root_budget,
            "caller_invocation_id": lease.identity.invocation_id,
            "depth": depth,
        }
        if lease.identity.origin_visible_tools is not None:
            payload["origin_visible_tools"] = list(lease.identity.origin_visible_tools)
        if lease.identity.origin_readonly is not None:
            payload["origin_readonly"] = lease.identity.origin_readonly
        if lease.identity.background_action_id is not None:
            payload["background_action_id"] = lease.identity.background_action_id
            payload["result_delivery"] = "requester"
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.events.append(EventDraft(EventType.TOOL_DEFERRED, "core", "plugin", payload))
        return ToolInvocationReceipt(request_id=request_id, tool_name=name, depth=depth)

    def _check_suspend_lease(self, lease: LeaseContext, method: str) -> None:
        """Refuse suspension from any lease that is not a tool callback's own."""
        if lease.identity.tool_callback and not lease.identity.hook and lease.identity.background_action_id is None:
            return
        if lease.identity.hook:
            kind = "hook"
        elif lease.identity.background_action_id is not None:
            kind = "background"
        elif lease.identity.actor == "user":
            kind = "command"
        else:
            kind = "non-tool"
        raise SuspendOutsideTool(f"{method} requires a tool-callback lease; {kind} leases cannot suspend because they have no run cursor to park")

    def _live_parked_ask(self, session_id: str) -> str | None:
        """The live parked ask request id for this session, if the store shows one.

        Advisory (no session lock): the engine park-commit re-checks the live
        binding authoritatively before committing anything.
        """
        store = self._runtime.require_store()
        try:
            session = store.session(session_id)
        except KeyError:
            return None
        if session.status is not SessionStatus.WAITING:
            return None
        checkpoint = store.checkpoint(session_id)
        if checkpoint is None or checkpoint.kind is CheckpointKind.TERMINAL or checkpoint.run_id != session.run_id:
            return None
        payload = checkpoint.payload
        candidate = payload.get("ask_request_id")
        return candidate if isinstance(candidate, str) and candidate else None

    def suspend_foreground_composition(self, draft: CompositionDraft, *, idempotency_key: str | None = None) -> NoReturn:  # noqa: ARG002
        """Stage this composition's wait-intent on the lease and raise SuspendComposition (never returns)."""
        self._active_lease()
        raise NotImplementedError(
            "suspend_foreground_composition is not implemented: the foreground composition slice "
            "(composition driver, composition rows, composition terminals) has not landed"
        )

    def suspend_for_ask(self, draft: AskRequestDraft, *, idempotency_key: str | None = None) -> NoReturn:
        """Validate refs/shape on the lease, stage the ask intent, raise SuspendAsk (never returns).

        Tool-callback leases only. Media digests are shape-checked here; existence
        is resolved by the plugin via read_artifact BEFORE suspending. A second
        suspend while the session holds a live park is refused with AskAlreadyParked.
        """
        lease = self._active_lease()
        self._check_suspend_lease(lease, "suspend_for_ask")
        park = SuspendAsk(draft, idempotency_key=idempotency_key)
        for reference in draft.media:
            if (
                not isinstance(reference, ArtifactReference)
                or not isinstance(reference.sha256, str)
                or SHA256_PATTERN.fullmatch(reference.sha256) is None
                or type(reference.bytes) is not int
                or reference.bytes < 0
                or not isinstance(reference.media_type, str)
                or not reference.media_type
            ):
                raise ValueError("ask media requires ArtifactReference entries with a lowercase sha256 digest, byte count, and media type")
        if lease.staging.suspend is not None:
            raise AskAlreadyParked("AskAlreadyParked: a suspension is already staged on this lease; refusing a second suspend")
        live_request = self._live_parked_ask(lease.identity.session_id)
        if live_request is not None:
            raise AskAlreadyParked(
                f"AskAlreadyParked: session {lease.identity.session_id!r} already holds live ask {live_request!r}; refusing a second suspend"
            )
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.suspend = park
        raise park

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None, include_context: bool = False) -> str:
        lease = self._active_lease()
        self._check_hook("model_query")
        callback = self._runtime.model_query_callback
        if callback is None:
            raise ModelQueryUnavailable("governed model queries are unavailable")
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or null")
        if max_output_tokens is not None and type(max_output_tokens) is not int:
            raise ValueError("max_output_tokens must be an integer or null")
        if type(include_context) is not bool:
            raise ValueError("include_context must be a boolean")
        return callback(
            lease.identity.session_id,
            str(prompt),
            model,
            max_output_tokens,
            include_context,
            lease.identity.owner,
            lease.abort_signal,
        )

    def model_query_structured(
        self,
        prompt: str,
        *,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> StructuredModelResult:
        lease = self._active_lease()
        self._check_hook("model_query_structured")
        callback = self._runtime.model_query_structured_callback
        if callback is None:
            raise ModelQueryUnavailable("governed model queries are unavailable")
        if not isinstance(result_schema, Mapping):
            raise ValueError("result_schema must be an object")
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or null")
        if max_output_tokens is not None and type(max_output_tokens) is not int:
            raise ValueError("max_output_tokens must be an integer or null")
        if type(include_context) is not bool:
            raise ValueError("include_context must be a boolean")
        return callback(
            lease.identity.session_id,
            str(prompt),
            result_schema,
            model,
            max_output_tokens,
            include_context,
            lease.identity.owner,
            lease.abort_signal,
        )


class _LeaseAbortSignal:
    """A ``ProviderAbortSignal`` view over one lease's runtime cancel event."""

    def __init__(self, event) -> None:  # type: ignore[no-untyped-def]
        self._event = event

    @property
    def cancelled(self) -> bool:
        return bool(self._event.is_set())

    @property
    def reason(self) -> str | None:
        return "cancelled" if self.cancelled else None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        del reason
        if value:
            self._event.set()
        else:
            self._event.clear()


class _CancelledAbortSignal:
    """An already-cancelled signal for a revoked parent lease."""

    @property
    def cancelled(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return "cancelled"

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        del value, reason
