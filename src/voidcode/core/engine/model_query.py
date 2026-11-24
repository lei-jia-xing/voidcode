"""Governed model queries: tool-free side turns with recorded usage.

The core orchestrator delegates side-channel queries (both unstructured and
JSON-Schema validated structured queries) to this runtime. Actual provider usage
is charged once to the session report and active owner budgets, but neither the
prompt nor the response extends conversation history.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import uuid4

import jsonschema

from ...sdk.json_values import json_wire_object, own_json_object
from ...sdk.provider import (
    ProviderAbortSignal,
    ProviderExecutionError,
    ProviderMessage,
    ProviderReasoningEffort,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from ...sdk.services import (
    ModelQueryCancelled,
    ModelQueryToolCalls,
    PluginBudget,
    SessionUsageReport,
    StructuredModelResult,
    StructuredQueryParseError,
)
from ..cancellation import Cancellation
from ..events import EventType
from ..execution import _Cursor, _Execution
from ..plugins import PluginRuntime
from ..storage.store import EventDraft, SessionRecord, SqliteStore
from ..usage import budget_block

MODEL_CONFIGURATION_GUIDANCE = "no model configured or registered"


class ModelQuerySnapshot(Protocol):
    """The capability snapshot one model query dispatch may use."""

    @property
    def plugin_runtime(self) -> PluginRuntime: ...


class ModelQueryHost(Protocol):
    """Explicit core primitives required by the model query runtime."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def default_model(self) -> str | None: ...

    @property
    def reasoning_effort(self) -> ProviderReasoningEffort | None: ...

    @property
    def abort_signal(self) -> Cancellation: ...

    def ensure_open(self) -> None: ...

    def session(self, session_id: str) -> SessionRecord: ...

    def messages(self, execution: _Execution, *, completed_only: bool, model: str) -> tuple[ProviderMessage, ...]: ...

    def dispatch(self, description: str) -> AbstractContextManager[ModelQuerySnapshot]: ...

    def session_budgets(self, session_id: str) -> Sequence[PluginBudget]: ...

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport: ...

    def commit_accounted_event(self, session_id: str, event: EventDraft) -> None: ...


class ModelQueryRuntime:
    """Executes governed unstructured and structured provider queries."""

    def __init__(self, host: ModelQueryHost) -> None:
        self._host = host

    def query(
        self,
        session_id: str,
        prompt: str,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> str:
        """A tool-free side turn, optionally reading the current conversation window.

        Actual provider usage is charged once to ``owner`` and the session; the
        question and answer never extend the conversation. The invoking lease's
        abort signal is authoritative over the core-wide default, so a cancelled
        command or run stops the side turn instead of waiting for its full reply.
        """
        turn, provider_name, model_name, elapsed_ms = self._turn(
            session_id, prompt, model, max_output_tokens, include_context, abort_signal=abort_signal
        )
        self._commit_usage(session_id, turn, provider_name, model_name, owner=owner, elapsed_ms=elapsed_ms)
        if self._abort_signal(abort_signal).cancelled:
            raise ModelQueryCancelled("model query cancelled")
        _reject_tool_calls(turn)
        return turn.output or ""

    def query_structured(
        self,
        session_id: str,
        prompt: str,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
        owner: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> StructuredModelResult:
        """A tool-free side turn parsed and validated against ``result_schema``.

        Same dispatch, abort, usage event, and tool-calls guard as ``query``.
        ``result_schema`` must be a JSON Schema describing one JSON object. An
        invalid reply yields a callable ``parse_error``; cancellation, budget
        exhaustion, and provider errors propagate exactly as the bare query —
        never as data.
        """
        if not isinstance(result_schema, Mapping):
            raise ValueError("result_schema must be an object")
        try:
            schema_json = json_wire_object(own_json_object(result_schema))
        except ValueError as exc:
            raise ValueError(f"result_schema must be a JSON object: {exc}") from exc
        try:
            jsonschema.Draft202012Validator.check_schema(schema_json)
        except jsonschema.exceptions.SchemaError as err:
            raise ValueError(f"result_schema is not a valid JSON schema: {err.message}") from err
        turn, provider_name, model_name, elapsed_ms = self._turn(
            session_id,
            f"{prompt}\n\nReply with exactly one JSON object and nothing else.",
            model,
            max_output_tokens,
            include_context,
            abort_signal=abort_signal,
        )
        self._commit_usage(session_id, turn, provider_name, model_name, owner=owner, elapsed_ms=elapsed_ms)
        if self._abort_signal(abort_signal).cancelled:
            raise ModelQueryCancelled("model query cancelled")
        _reject_tool_calls(turn)
        raw_text = turn.output or ""
        stripped = raw_text.strip()
        if not stripped:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="invalid_json", message="model reply is empty"),
                done_reason=turn.done_reason,
            )
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="invalid_json", message=f"model reply is not JSON: {exc.msg}"),
                done_reason=turn.done_reason,
            )
        if not isinstance(parsed, dict):
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="schema_violation", message="top-level JSON value must be an object"),
                done_reason=turn.done_reason,
            )
        try:
            owned = own_json_object(parsed)
        except ValueError as exc:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="invalid_json", message=f"model reply is not JSON: {exc}"),
                done_reason=turn.done_reason,
            )
        try:
            jsonschema.Draft202012Validator(schema_json).validate(json_wire_object(owned))
        except jsonschema.ValidationError as exc:
            return StructuredModelResult(
                raw_text=raw_text,
                parse_error=StructuredQueryParseError(kind="schema_violation", message=exc.message, path=exc.json_path),
                done_reason=turn.done_reason,
            )
        return StructuredModelResult(raw_text=raw_text, data=owned, done_reason=turn.done_reason)

    def _turn(
        self,
        session_id: str,
        prompt: str,
        model: str | None,
        max_output_tokens: int | None,
        include_context: bool,
        *,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[ProviderTurnResult, str, str, int]:
        """The shared governed side-turn: resolution, budget, dispatch, and measurement."""
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 64 * 1024:
            raise ValueError("model query prompt must be nonempty UTF-8 text within 64 KiB")
        self._host.ensure_open()
        self._host.session(session_id)
        effective_abort = self._abort_signal(abort_signal)
        if effective_abort.cancelled:
            raise ModelQueryCancelled("model query cancelled")
        selected = model or self._host.default_model
        if selected is None:
            raise ValueError(MODEL_CONFIGURATION_GUIDANCE)
        provider_name, separator, model_name = selected.partition("/")
        with self._host.dispatch("model-query") as snapshot:
            if not separator or not model_name or provider_name not in snapshot.plugin_runtime.providers:
                raise ValueError("model must name a configured provider/model")
            metadata: dict[str, object] = {"session_id": session_id, "purpose": "plugin_query"}
            if max_output_tokens is not None:
                if type(max_output_tokens) is not int or max_output_tokens < 1:
                    raise ValueError("max_output_tokens must be a positive integer")
                metadata["max_output_tokens"] = max_output_tokens
            self._admit_budget(session_id)
            context: tuple[ProviderMessage, ...] = ()
            if include_context:
                session = self._host.store.session(session_id)
                execution = _Execution(session, _Cursor(), list(self._host.store.events(session_id)), session.run_id or uuid4().hex)
                context = self._host.messages(execution, completed_only=True, model=selected)
            request = ProviderTurnRequest(
                messages=(*context, ProviderMessage("user", prompt)),
                available_tools=(),
                raw_model=selected,
                provider_name=provider_name,
                model_name=model_name,
                reasoning_effort=self._host.reasoning_effort,
                metadata=metadata,
                abort_signal=effective_abort,
            )
            started = time.monotonic()
            try:
                turn = snapshot.plugin_runtime.propose_turn(request, provider=provider_name)
            except ProviderExecutionError as exc:
                if exc.kind == "cancelled":
                    raise ModelQueryCancelled("model query cancelled") from exc
                raise
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return turn, provider_name, model_name, elapsed_ms

    def _abort_signal(self, abort_signal: ProviderAbortSignal | None) -> ProviderAbortSignal:
        """Use the invoking lease's signal, falling back to the core-wide default."""
        return self._host.abort_signal if abort_signal is None else abort_signal

    def _admit_budget(self, session_id: str) -> None:
        """Refuse a side query when an active session/plugin budget already blocks dispatch."""
        for budget in self._host.session_budgets(session_id):
            report = self._host.usage(session_id, since_sequence=budget.since_sequence)
            block = budget_block(report, budget)
            if block is not None:
                raise RuntimeError(block.message)

    def _commit_usage(
        self,
        session_id: str,
        turn: ProviderTurnResult,
        provider_name: str,
        model_name: str,
        *,
        owner: str | None,
        elapsed_ms: int,
    ) -> None:
        """Record one governed query's usage; the question and answer stay out of history."""
        payload: dict[str, object] = {
            "provider": provider_name,
            "model": model_name,
            "usage": None if turn.usage is None else turn.usage.metadata_payload(),
            "cost_usd": None if turn.usage is None else turn.usage.cost_usd,
            "elapsed_ms": elapsed_ms,
        }
        if owner is not None:
            payload["owner"] = owner
        self._host.commit_accounted_event(session_id, EventDraft(EventType.MODEL_QUERY, "core", "plugin", payload))


def _reject_tool_calls(turn: ProviderTurnResult) -> None:
    """A side query offers no tools, so any native call is a provider contract failure."""
    if turn.tool_calls:
        raise ModelQueryToolCalls("governed model query returned tool calls; none were offered")
