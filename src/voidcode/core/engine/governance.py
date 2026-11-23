"""Tool governance, visibility filtering, approval policy, and request measurement."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal, Protocol

import jsonschema

from ...sdk.codec import encode_tool_definition
from ...sdk.hooks import RequestWindow, WindowSegment
from ...sdk.json_values import json_wire_object
from ...sdk.provider import ProviderMessage
from ...sdk.tools import (
    AttachmentOutput,
    CompositeOutput,
    EmptyOutput,
    TextOutput,
    TextPart,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolOutput,
    ToolResult,
    composite_ordered_text,
    denied_under_restriction,
    has_external_mutations,
)
from ..events import EventType
from ..execution import _Execution
from ..plugins import PluginRuntime
from ..storage.store import EventDraft, SqliteStore, canonical_json

#: A hook note/failure is durable evidence, not a result body: bound it so a broken or
#: verbose hook cannot grow an event without limit. The plugin's own 2000-character
#: note bound stays authoritative for what it returns; this is the core's last line.
_HOOK_EVENT_TEXT_BYTES = 8192
_HOOK_EVENT_TRUNCATION = "\n[truncated]"


def _bounded_hook_text(text: str) -> tuple[str, bool]:
    """Return UTF-8-bounded text and whether it was clipped for the durable event."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _HOOK_EVENT_TEXT_BYTES:
        return encoded.decode("utf-8"), False
    clipped = encoded[:_HOOK_EVENT_TEXT_BYTES].decode("utf-8", errors="ignore")
    return f"{clipped}{_HOOK_EVENT_TRUNCATION}", True


def _append_output_note(output: ToolOutput, note: str) -> ToolOutput:
    """Append one hook note without changing a result output's type or content.

    Every member of the closed ``ToolOutput`` union has a safe note slot: text results
    append text, composite results append one ``TextPart`` (never dropping media, table,
    or JSON parts), and presentation-only results extend their presentation. Bounds and
    references stay attached to the original output. An output outside that union is a
    producer contract violation; refuse it visibly instead of flattening it to text.
    """
    if isinstance(output, TextOutput):
        return replace(output, text=f"{output.text}\n\n{note}" if output.text else note)
    if isinstance(output, CompositeOutput):
        return replace(output, parts=(*output.parts, TextPart(note)))
    if isinstance(output, (EmptyOutput, AttachmentOutput)):
        return replace(output, presentation=f"{output.presentation}\n\n{note}" if output.presentation else note)
    raise TypeError(f"post-tool hook cannot append a note to output type {type(output).__name__}")


def _hook_observation_text(output: ToolOutput) -> str:
    """The readable result text one post-tool hook observes; never a hook's own note."""
    if isinstance(output, TextOutput):
        return output.text
    if isinstance(output, CompositeOutput):
        return composite_ordered_text(output)
    return output.presentation or ""


def _lifecycle_hook_recorded(execution: _Execution, kind: str, owner: str) -> bool:
    """Whether this run already has a durable lifecycle record for this owner and kind.

    The event is written after the hook runs, so it is exactly the guard that prevents a
    same-run continuation/crash re-entry from firing a side effect twice.
    """
    return any(
        event.event_type == EventType.HOOK_INVOKED
        and event.run_id == execution.run_id
        and event.payload.get("kind") == kind
        and event.payload.get("owner") == owner
        for event in execution.events
    )


def _hook_event(
    execution: _Execution,
    kind: str,
    owner: str,
    *,
    outcome: Literal["noted", "silent", "failed"],
    note: str = "",
    failure: str = "",
    status: str = "",
) -> EventDraft:
    """Build one durable hook outcome event with bounded text fields."""
    payload: dict[str, object] = {
        "kind": kind,
        "owner": owner,
        "logical_turn": execution.cursor.logical_turn,
        "status": status,
        "outcome": outcome,
    }
    if note:
        bounded, truncated = _bounded_hook_text(note)
        payload["note"] = bounded
        payload["note_truncated"] = truncated
    if failure:
        bounded, truncated = _bounded_hook_text(failure)
        payload["failure"] = bounded
        payload["failure_truncated"] = truncated
    return EventDraft(EventType.HOOK_INVOKED, "core", "plugin", payload)


def tool_schema_characters(tools: tuple[ToolDefinition, ...]) -> int:
    """Characters of the core-side neutral tool encoding this dispatch advertises."""
    return sum(len(canonical_json(encode_tool_definition(tool))) for tool in tools)


def request_window(
    messages: tuple[ProviderMessage, ...],
    tools: tuple[ToolDefinition, ...],
) -> RequestWindow:
    """Measure the request exactly as it is about to be sent (core truth, no estimates).

    Per-segment facts are character counts and digests of the literal strings, so a client
    panel derives its view from measurements; token counts stay provider-reported usage.
    Image inventory counts carried attachments and their base64-text bytes, never tokens.
    """
    segments: list[WindowSegment] = []
    message_characters = 0
    for message in messages:
        content = message.content or ""
        characters = len(content)
        message_characters += characters
        segments.append(
            WindowSegment(
                role=message.role,
                characters=characters,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                tool_calls=len(message.tool_calls),
                tool_name=message.tool_name,
                reasoning_characters=len(message.reasoning or ""),
                image_count=len(message.images),
                image_bytes=sum(len(image.data.encode("utf-8")) for image in message.images),
            )
        )
    schema_chars = tool_schema_characters(tools)
    return RequestWindow(
        message_characters=message_characters,
        tool_schema_characters=schema_chars,
        segments=tuple(segments),
        tool_count=len(tools),
    )


def validate_steps(max_steps: int | None) -> None:
    if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
        raise ValueError("max_steps must be a positive integer or null")


def validate_interaction(interaction: Literal["interactive", "unattended"] | None) -> None:
    if interaction is not None and interaction not in ("interactive", "unattended"):
        raise ValueError('interaction must be "interactive", "unattended", or null')


def validate_root_action_budget(budget: int | None) -> None:
    if budget is not None and (type(budget) is not int or not 0 <= budget <= 9_223_372_036_854_775_807):
        raise ValueError("root_action_budget must fit a non-negative SQLite INTEGER")


def validate_root_action_extension(amount: int | None, grant_id: str | None) -> None:
    if amount is None:
        if grant_id is not None:
            raise ValueError("root_action_grant_id requires extend_root_actions")
        return
    if type(amount) is not int or amount < 1:
        raise ValueError("extend_root_actions must be a positive integer")
    if not isinstance(grant_id, str) or not grant_id or len(grant_id) > 128 or "\0" in grant_id:
        raise ValueError("root_action_grant_id must be nonempty text within 128 characters")


class ToolsSnapshot(Protocol):
    """The capability snapshot tools query."""

    @property
    def tools(self) -> tuple[ToolDefinition, ...]: ...


class GovernanceSnapshot(ToolsSnapshot, Protocol):
    """The capability snapshot tool governance operations inspect."""

    @property
    def revision_id(self) -> str: ...

    @property
    def plugin_runtime(self) -> PluginRuntime: ...

    def definition_of(self, name: str) -> ToolDefinition | None: ...

    def validator_of(self, name: str) -> Any | None: ...


class GovernanceHost(Protocol):
    """Host requirements for tool governance."""

    @property
    def store(self) -> SqliteStore: ...

    @property
    def policy_read_only(self) -> bool: ...

    @property
    def approval_mode(self) -> str: ...

    @property
    def snapshot(self) -> GovernanceSnapshot | None: ...

    def commit_hook_events(self, execution: _Execution, events: tuple[EventDraft, ...]) -> None:
        """Commit hook bookkeeping events without advancing the checkpoint.

        Lifecycle hooks are observational: their outcome must be durable before the
        run continues, but must not change the run's decision or checkpoint.
        """
        ...


class ToolGovernance:
    """Enforces tool visibility, pre-tool gates, approval modes, and post-tool hooks."""

    def __init__(self, host: GovernanceHost) -> None:
        self._host = host

    def session_read_only(self, session_id: str) -> bool:
        """Effective read-only for one session: the frozen core policy OR owner overlays."""
        if self._host.policy_read_only:
            return True
        return bool(self._host.store.session_overlays(session_id))

    def visible_tools(self, execution: _Execution, snapshot: GovernanceSnapshot) -> tuple[ToolDefinition, ...]:
        """The catalog this session may see: current scope intersected with captured origin authority."""
        tools = snapshot.tools
        scope = execution.session.tool_allowlist
        if scope is not None:
            tools = tuple(tool for tool in tools if tool.name in scope)
        if execution.cursor.deferred_origin_tools is not None:
            allowed = set(execution.cursor.deferred_origin_tools)
            tools = tuple(tool for tool in tools if tool.name in allowed)
        restrictive = self.session_read_only(execution.session.session_id) or execution.cursor.deferred_origin_readonly is True
        if restrictive:
            tools = tuple(tool for tool in tools if not denied_under_restriction(tool.effects))
        return tools

    def visible_tool_names_for_session(self, session_id: str, snapshot: ToolsSnapshot) -> tuple[str, ...]:
        session = self._host.store.session(session_id)
        tools = snapshot.tools
        if session.tool_allowlist is not None:
            tools = tuple(tool for tool in tools if tool.name in session.tool_allowlist)
        if self.session_read_only(session_id):
            tools = tuple(tool for tool in tools if not denied_under_restriction(tool.effects))
        return tuple(tool.name for tool in tools)

    def visible_tool_definition(self, session_id: str, name: str, revision_id: str) -> tuple[Mapping[str, object] | None, str]:
        """One definition from the active revision's dispatch snapshot, with this session's filters.

        The same source and the same predicate ``visible_tools`` applies: the session's profile
        scope first, then the effective read-only stance (frozen policy or owner overlays). It
        answers ``(None, reason)`` instead of raising, so a documentation read reports the actual
        visibility instead of guessing it.
        """
        snapshot = self._host.snapshot
        if snapshot is None or snapshot.revision_id != revision_id:
            raise ValueError("the lease's revision is no longer the active capability revision")
        definition = snapshot.definition_of(name)
        if definition is None:
            return None, "unknown"
        session = self._host.store.session(session_id)
        scope = session.tool_allowlist
        if scope is not None and name not in scope:
            return None, "scope"
        if self.session_read_only(session_id) and denied_under_restriction(definition.effects):
            return None, "read_only"
        return encode_tool_definition(definition), "visible"

    def denial(self, call: ToolCall, definition: ToolDefinition, *, read_only: bool | None = None) -> str | None:
        snapshot = self._host.snapshot
        validator = None if snapshot is None else snapshot.validator_of(definition.name)
        if validator is not None:
            try:
                validator.validate(json_wire_object(call.arguments))
            except jsonschema.ValidationError:
                return "tool arguments failed the admitted JSON schema"
        # read_only denies EXTERNAL mutation only: reads and internal state
        # operations (SESSION) stay available in every mode. The session's effective
        # restriction includes its owner overlays (plan mode) as well as the frozen policy.
        restricted = self._host.policy_read_only if read_only is None else read_only
        if restricted and denied_under_restriction(definition.effects):
            return "read-only policy denies the declared tool effects"
        return None

    def needs_approval(self, definition: ToolDefinition) -> bool:
        effects = definition.effects
        if not has_external_mutations(effects):
            # Reads and internal state operations (SESSION) mutate nothing
            # external, so no mode gates them behind a human decision.
            return False
        mode = self._host.approval_mode
        if mode == "yolo":
            return False
        return not (mode == "write" and effects and effects <= {ToolEffect.READ, ToolEffect.WRITE})

    def gate_denial(
        self,
        execution: _Execution,
        snapshot: GovernanceSnapshot,
        call: ToolCall,
        *,
        readonly: bool,
    ) -> str | None:
        """Ask every declared pre-tool gate; failure or a stated reason denies the call.

        Fail-closed by construction: a gate that errors, times out, or vanishes denies, because
        a gate exists to refuse work and a broken gate must never be read as permission.
        """
        gates = [hook for hook in snapshot.plugin_runtime.pre_tool_hooks if call.tool_name in hook[1] or "*" in hook[1]]
        for owner, _match, mutating in gates:
            if mutating and readonly:
                # A mutating gate is not asked under a restrictive stance. Record the skip
                # durably (best effort) because a denial cannot carry a non-denial note:
                # returning a skip string here would refuse the call and change semantics.
                try:
                    self._host.commit_hook_events(
                        execution,
                        (
                            EventDraft(
                                EventType.HOOK_SKIPPED,
                                "core",
                                "core",
                                {
                                    "kind": "pre_tool",
                                    "owner": owner,
                                    "reason": "read_only",
                                    "tool_name": call.tool_name,
                                    "tool_call_id": call.tool_call_id,
                                    "logical_turn": execution.cursor.logical_turn,
                                },
                            ),
                        ),
                    )
                except Exception:
                    # Visibility is best effort; the gate outcome (allow) is unchanged.
                    pass
                continue
            try:
                reason = snapshot.plugin_runtime.pre_tool(
                    owner,
                    session_id=execution.session.session_id,
                    run_id=execution.run_id,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    arguments=call.arguments,
                    readonly=readonly,
                    logical_turn=execution.cursor.logical_turn,
                )
            except Exception as exc:
                return f"pre-tool gate {owner} failed: {type(exc).__name__}: {exc}"
            if reason:
                return f"pre-tool gate {owner} denied the call: {reason}"
        return None

    def post_tool_hooks(
        self,
        execution: _Execution,
        snapshot: GovernanceSnapshot,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        """Run declared post-tool hooks inside this invocation, before its result is durable.

        Only a successful result is offered: a hook observes what the tool actually did and
        appends one note (a hook reporting what it observed), never a rewritten verdict.
        A hook failure is recorded as a visible note on the same result, so a broken hook
        cannot silently disappear from the record the model and the events both carry.
        """
        if isinstance(result, ToolFailure):
            return result
        text = _hook_observation_text(result.output)
        candidates = [hook for hook in snapshot.plugin_runtime.post_tool_hooks if call.tool_name in hook[1] or "*" in hook[1]]
        if not candidates:
            return result
        restricted = self.session_read_only(execution.session.session_id)
        notes: list[str] = []
        for owner, _match, mutating in candidates:
            if mutating and restricted:
                # A mutating hook cannot run under a restrictive stance: formatting writes files.
                notes.append(f"[hook {owner} skipped: read-only policy]")
                continue
            try:
                note = snapshot.plugin_runtime.post_tool(
                    owner,
                    session_id=execution.session.session_id,
                    run_id=execution.run_id,
                    tool_name=call.tool_name,
                    tool_call_id=call.tool_call_id,
                    status="ok",
                    text=text,
                    arguments=call.arguments,
                    readonly=restricted,
                    logical_turn=execution.cursor.logical_turn,
                )
            except Exception as exc:
                notes.append(f"[hook {owner} failed: {type(exc).__name__}: {exc}]")
                continue
            if note:
                notes.append(note)
        if not notes:
            return result
        # The hook observes a settled result: its note is appended, never substituted. Keep
        # the output type, all typed parts, bounds/references, body, coverage, changes,
        # steering context, and result_reference exactly as the tool produced them.
        return replace(result, output=_append_output_note(result.output, "\n\n".join(notes)))

    def lifecycle_events(
        self,
        execution: _Execution,
        snapshot: GovernanceSnapshot,
        kind: Literal["session_start", "session_end"],
        *,
        status: Literal["completed", "failed", "interrupted", ""] = "",
        error: str | None = None,
    ) -> tuple[EventDraft, ...]:
        """Notify declared lifecycle hooks and make each outcome durable.

        A hook's note or failure never changes the run's decision. Every invocation
        appends one ``core.hook_invoked`` event (bounded) carrying the note, the failure,
        or ``silent``; an owner/kind already recorded for this run is never asked again.
        ``session_start`` events are committed immediately because no terminal commit
        may ever follow. ``session_end`` events are returned for the caller's terminal
        commit so the end record and the run's terminal fact are one atomic append.
        """
        hooks = [owner for owner, hook_kind in snapshot.plugin_runtime.lifecycle_hooks if hook_kind == kind]
        drafts: list[EventDraft] = []
        for owner in hooks:
            if _lifecycle_hook_recorded(execution, kind, owner):
                continue
            try:
                note = snapshot.plugin_runtime.run_lifecycle(
                    owner,
                    kind,
                    session_id=execution.session.session_id,
                    run_id=execution.run_id,
                    logical_turn=execution.cursor.logical_turn,
                    status=status,
                    error=error,
                )
            except Exception as exc:
                drafts.append(
                    _hook_event(
                        execution,
                        kind,
                        owner,
                        outcome="failed",
                        failure=f"{type(exc).__name__}: {exc}",
                        status=status,
                    )
                )
                continue
            drafts.append(
                _hook_event(
                    execution,
                    kind,
                    owner,
                    outcome="noted" if note else "silent",
                    note=note or "",
                    status=status,
                )
            )
        if kind == "session_start":
            if drafts:
                self._host.commit_hook_events(execution, tuple(drafts))
            return ()
        return tuple(drafts)
