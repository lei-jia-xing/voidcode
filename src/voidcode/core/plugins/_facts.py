"""Observer fact delivery, typed fact decoding, and fact page reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import TypeAdapter

from voidcode.sdk.hooks import (
    ApprovalRequestedFact,
    ChildSpawnedFact,
    ContextProjectedFact,
    FactPage,
    InvocationFinishedFact,
    ObservedFactKind,
    PluginFact,
    RunFinishedFact,
    RunRequestedFact,
    UsageFact,
    WakeupFiredFact,
    is_stop_cause,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import (
    ProviderTokenUsage,
)
from voidcode.sdk.services import (
    BudgetExhaustion,
    PluginBudget,
)

from ..events import EventType
from ..facts import fact_for_event
from ..services import RuntimeServices
from ..storage.store import (
    EventDraft,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
)
from ._contracts import CallbackChanges

if TYPE_CHECKING:
    from . import PluginRuntime
from ._dispatch import PluginDispatch


class PluginFacts(PluginDispatch):
    """Observer fact delivery, typed fact decoding, and fact page reads."""

    def fact_page(self, session_id: str, after: int, limit: int) -> FactPage:
        store = self.require_store()
        source = store.events(session_id, after_sequence=after, limit=limit + 1)
        page = source[:limit]
        facts: list[PluginFact] = []
        for event in page:
            context = ()
            dispatch = event.payload.get("dispatch_sequence")
            if event.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED) and type(dispatch) is int:
                context = store.events(session_id, after_sequence=dispatch - 1, limit=1)
            decoded = fact_for_event(event, context)
            if decoded is not None:
                kind, payload = decoded
                facts.append(self._typed_fact(kind, payload, event.session_id, event.run_id, event.revision_id, str(payload.get("logical_turn", ""))))
        return FactPage(tuple(facts), page[-1].sequence if page else after, len(source) > limit)

    def notify_fact(
        self: PluginRuntime,
        fact_type: ObservedFactKind,
        fact: Mapping[str, object],
        *,
        owners: Sequence[str] | None = None,
        logical_turn: str = "",
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
    ) -> CallbackChanges:
        """Notify subscribers of one fact; `owners` restricts delivery to those captured subscribers."""
        subscribed_owners = self._observer_owners.get(fact_type, ())
        if owners is not None:
            requested = frozenset(owners)
            subscribed_owners = tuple(owner for owner in subscribed_owners if owner in requested)
        if not subscribed_owners:
            return CallbackChanges()

        all_state_updates: list[StagedStateUpdate] = []
        all_events: list[EventDraft] = []
        all_conts: list[StagedContinuationDraft] = []
        all_cancels: list[StagedCancellation] = []
        all_wakeups: list[StagedWakeup] = []
        all_wakeup_cancels: list[StagedWakeupCancel] = []
        all_budgets: list[tuple[str, PluginBudget | Literal["clear"]]] = []
        all_errors: list[dict[str, str]] = []

        session_id = _required_fact_field(fact, "session_id")
        run_id = _required_fact_field(fact, "run_id")
        revision_id = _required_fact_field(fact, "revision_id")
        typed = self._typed_fact(fact_type, fact, session_id, run_id, revision_id, str(fact.get("logical_turn", logical_turn)))

        for owner in subscribed_owners:
            lease = self._open_lease(
                owner=owner,
                session_id=session_id,
                run_id=run_id,
                invocation_id=uuid4().hex,
                readonly=False,
                actor="plugin",
                root_action_id=root_action_id,
                initial_root_budget=initial_root_budget,
            )
            context = ToolContext(
                workspace=self.workspace,
                session_id=session_id,
                run_id=run_id,
                invocation_id=lease.identity.invocation_id,
                revision_id=revision_id,
                actor="plugin",
                logical_turn=str(fact.get("logical_turn", logical_turn)),
            )
            services = RuntimeServices(self, lease)
            try:
                for obs, observed_kinds, plugin_id in self._observers:
                    if plugin_id != owner or fact_type not in observed_kinds:
                        continue
                    try:
                        obs(typed, context, services)
                    except Exception as exc:
                        # A raised observer callback invalidates every proposal it staged:
                        # truth (bill/fact) stays, its partial side effects do not.
                        all_errors.append({"owner": plugin_id, "error": f"{type(exc).__name__}: {exc}"})
                        break
                else:
                    owner_schema_ver = self.state_schema_version(lease.identity.owner)
                    all_state_updates.extend(
                        StagedStateUpdate(
                            owner=lease.identity.owner,
                            scope=sc,
                            scope_key=sk,
                            payload=st,
                            expected_version=baseline,
                            schema_version=owner_schema_ver,
                            revision_id=self.revision_id,
                        )
                        for (sc, sk), (st, _local, baseline) in lease.staging.state.items()
                    )
                    all_events.extend(lease.staging.events)
                    all_conts.extend(lease.staging.continuations)
                    all_cancels.extend(lease.staging.cancellations)
                    all_wakeups.extend(lease.staging.wakeups)
                    all_wakeup_cancels.extend(lease.staging.wakeup_cancels)
                    if lease.staging.budget is not None:
                        all_budgets.append((lease.identity.owner, lease.staging.budget))
                    continue
            finally:
                self._revoke_lease(lease)

        return CallbackChanges(
            staged_state=tuple(all_state_updates),
            staged_events=tuple(all_events),
            staged_continuations=tuple(all_conts),
            staged_cancellations=tuple(all_cancels),
            staged_wakeups=tuple(all_wakeups),
            staged_wakeup_cancels=tuple(all_wakeup_cancels),
            staged_budgets=tuple(all_budgets),
            errors=tuple(all_errors),
        )

    @staticmethod
    def _typed_fact(
        fact_type: ObservedFactKind,
        fact: Mapping[str, object],
        session_id: str,
        run_id: str,
        revision_id: str,
        logical_turn: str,
    ) -> PluginFact:
        if fact_type == "usage":
            usage_payload = fact.get("usage")
            usage_obj = None
            if isinstance(usage_payload, Mapping):
                usage_obj = ProviderTokenUsage(
                    input_tokens=usage_payload.get("input_tokens"),
                    output_tokens=usage_payload.get("output_tokens"),
                    cache_read_tokens=usage_payload.get("cache_read_tokens"),
                    cache_write_tokens=usage_payload.get("cache_write_tokens"),
                    uncached_input_tokens=usage_payload.get("uncached_input_tokens"),
                    cost_usd=_optional_number(usage_payload, "cost_usd"),
                )
            raw_source = fact.get("source", "turn")
            source: Literal["turn", "model_query"]
            if raw_source == "turn":
                source = "turn"
            elif raw_source == "model_query":
                source = "model_query"
            else:
                raise ValueError(f"Unknown usage fact source: {raw_source!r}")
            owner = fact.get("owner")
            if owner is not None and not isinstance(owner, str):
                raise ValueError("'owner' must be a string or null")
            return UsageFact(
                session_id=session_id,
                run_id=run_id,
                revision_id=revision_id,
                logical_turn=logical_turn,
                event_sequence=_required_int(fact, "event_sequence"),
                dispatch_sequence=_required_int(fact, "dispatch_sequence"),
                provider_name=_required_text(fact, "provider_name"),
                model_name=_required_text(fact, "model_name"),
                usage=usage_obj,
                input_limit_tokens=_optional_positive_int(fact, "input_limit_tokens"),
                context_window=_optional_positive_int(fact, "context_window"),
                elapsed_seconds=_optional_number(fact, "elapsed_seconds"),
                cost_usd=_optional_number(fact, "cost_usd"),
                source=source,
                owner=owner,
            )
        if fact_type == "invocation_finished":
            call_id = fact.get("tool_call_id")
            if call_id is not None and not isinstance(call_id, str):
                raise ValueError("'tool_call_id' must be a string or null")
            raw_status = fact.get("status")
            if raw_status not in ("ok", "error", "cancelled"):
                raise ValueError(f"Unknown invocation status: {raw_status!r}")
            error = fact.get("error")
            if error is not None and not isinstance(error, str):
                raise ValueError("'error' must be a string or null")
            raw_effects = fact.get("effects", ())
            if not isinstance(raw_effects, (list, tuple)) or not all(isinstance(item, str) for item in raw_effects):
                raise ValueError("'effects' must be a list of strings")
            return TypeAdapter(InvocationFinishedFact).validate_python(json_wire_object(fact))
        extra_types: dict[ObservedFactKind, type[PluginFact]] = {
            "run_requested": RunRequestedFact,
            "approval_requested": ApprovalRequestedFact,
            "child_spawned": ChildSpawnedFact,
            "context_projected": ContextProjectedFact,
            "wakeup_fired": WakeupFiredFact,
        }
        if fact_type in extra_types:
            return TypeAdapter(extra_types[fact_type]).validate_python(json_wire_object(fact))
        if fact_type == "run_finished":
            stop_cause_payload = fact.get("stop_cause")
            if stop_cause_payload is not None and not is_stop_cause(stop_cause_payload):
                raise ValueError(f"Unknown 'stop_cause': {stop_cause_payload!r}")
            status_payload = _required_text(fact, "status")
            if status_payload not in ("completed", "failed", "interrupted"):
                raise ValueError(f"Unknown run status: {status_payload!r}")
            return RunFinishedFact(
                session_id=session_id,
                run_id=run_id,
                revision_id=revision_id,
                logical_turn=logical_turn,
                event_sequence=_required_int(fact, "event_sequence"),
                status=status_payload,
                stop_cause=stop_cause_payload,
                elapsed_seconds=_optional_number(fact, "elapsed_seconds"),
                budget_exhaustion=BudgetExhaustion.from_payload(fact.get("budget_exhaustion")),
            )
        raise ValueError(f"Unknown fact_type: {fact_type}")


def _required_fact_field(fact: Mapping[str, object], field_name: str) -> str:
    """Read one required observer-fact identity field, failing visibly when absent."""
    value = fact.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"observer fact requires a non-empty {field_name!r}")
    return value


def _required_text(params: Mapping[str, Any], key: str) -> str:
    """Read a required non-empty text parameter, failing visibly instead of defaulting."""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' parameter required as non-empty string")
    return value


def _optional_text(params: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read an optional text parameter; an empty string is an explicit 'no value'."""
    value = params.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' parameter must be a string")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    """Read a required integer field without coercing strings or booleans."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{key}' must be an integer")
    return value


def _optional_positive_int(payload: Mapping[str, Any], key: str) -> int | None:
    """Read an optional positive integer field, refusing booleans and coercions."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"'{key}' must be a positive integer or null")
    return value


def _optional_number(payload: Mapping[str, Any], key: str) -> float | None:
    """Read an optional numeric field without coercing strings or booleans."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{key}' must be a number or null")
    return float(value)
