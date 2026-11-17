"""Shared FakeServices fixture for unit testing plugins."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import jsonschema
import pytest

from voidcode.core.storage.store import StoredEvent
from voidcode.core.transcript import context_records
from voidcode.sdk.history import HistoryPage
from voidcode.sdk.hooks import FactPage
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.resources import ResourcePage, ResourceReadRequest, ResourceUnavailable
from voidcode.sdk.services import (
    ArtifactPage,
    ArtifactReference,
    ChildOutcome,
    ChildSendReceipt,
    DescendantNode,
    EventView,
    PluginBudget,
    PluginServices,
    ProcessHandle,
    ProcessOutput,
    ProcessResult,
    SessionInfo,
    SessionUsageReport,
    StructuredModelResult,
    StructuredQueryParseError,
    ToolInvocationReceipt,
)
from voidcode.sdk.tools import ToolDefinition
from voidcode.sdk.transcript import ContextHistory, TranscriptPage


def _event_size(event: object) -> int:
    payload = getattr(event, "payload", {})
    return len(
        json.dumps(
            json_wire_object(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


@dataclass
class FakeServices(PluginServices):
    """Clean, minimal in-memory implementation of PluginServices for unit tests."""

    events: Sequence[object] = field(default_factory=list)
    owner: str = "test"
    session_id: str = "session-1"
    workspace_root: Path = field(default_factory=Path.cwd)
    states: dict[tuple[str, str, str], tuple[int, dict[str, object]]] = field(default_factory=dict)
    staged_events: list[object] = field(default_factory=list)
    staged_continuations: list[object] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)

    facts: list[object] = field(default_factory=list)
    through_sequence: int = 0
    cancelled_continuations: list[str | None] = field(default_factory=list)

    budget: PluginBudget | None = None
    overlay: str | None = None

    model_answer: str | None = None
    answer: str | None = None
    structured_answer: StructuredModelResult | None = None
    model_error: Exception | None = None
    failure: Exception | None = None
    refuse: bool = False
    prompts: list[str] = field(default_factory=list)

    projections: list[dict[str, object]] = field(default_factory=list)
    row: dict[str, object] | None = None

    spawn_calls: list[dict[str, object]] = field(default_factory=list)
    spawn_failures: dict[int, str | Exception] = field(default_factory=dict)
    join_calls: list[tuple[str, float]] = field(default_factory=list)
    wait_calls: list[tuple[str, ...]] = field(default_factory=list)
    join_status: Literal["completed", "failed", "interrupted", "cancelled", "running", "waiting"] = "completed"
    join_output: str | None = None
    join_error: str | None = None
    roster: dict[str, ChildOutcome] = field(default_factory=dict)
    send_calls: list[dict[str, object]] = field(default_factory=list)
    send_result: ChildSendReceipt = field(
        default_factory=lambda: ChildSendReceipt(child_session_id="child-1", delivery="queued", child_status="running")
    )
    acks: list[tuple[str, str]] = field(default_factory=list)
    cancel_calls: list[tuple[str, str]] = field(default_factory=list)
    cancel_result: bool = True
    child_events: dict[str, tuple[EventView, ...]] = field(default_factory=dict)

    wakeups: dict[str, dict[str, object]] = field(default_factory=dict)
    unavailable_tools: set[str] = field(default_factory=set)
    tool_definitions: dict[str, ToolDefinition] = field(default_factory=dict)
    artifacts: dict[str, bytes] = field(default_factory=dict)
    queries_dict: dict[str, Mapping[str, object] | None] = field(default_factory=dict)
    resources_dict: dict[str, ResourcePage | None] = field(default_factory=dict)
    processes: dict[str, ProcessHandle] = field(default_factory=dict)
    process_result: ProcessResult | None = None

    def __post_init__(self) -> None:
        if isinstance(self.events, tuple):
            self.events = list(self.events)
        if self.answer is None and self.model_answer is not None:
            self.answer = self.model_answer
        if self.model_answer is None and self.answer is not None:
            self.model_answer = self.answer
        if self.failure is None and self.model_error is not None:
            self.failure = self.model_error
        if self.model_error is None and self.failure is not None:
            self.model_error = self.failure

    # --- Convenience properties & adapters ---

    @property
    def state(self) -> Mapping[str, object] | None:
        return self.read_state("session")[0]

    @property
    def staged(self) -> list[object]:
        return self.staged_events

    @staged.setter
    def staged(self, value: list[object]) -> None:
        self.staged_events = value

    @property
    def queries(self) -> list[str]:
        return self.prompts

    @queries.setter
    def queries(self, value: list[str]) -> None:
        self.prompts = value

    # --- State operations ---

    def read_state(
        self,
        scope: str = "session",
        owner: str | None = None,
    ) -> tuple[Mapping[str, object] | None, int]:
        key = (self.session_id, owner or self.owner, scope)
        if key in self.states:
            ver, payload = self.states[key]
            return (dict(payload) if payload else None), ver
        return None, 0

    get_state = read_state

    def replace_state(
        self,
        state: Mapping[str, object] | None,
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
        owner: str | None = None,
    ) -> int:
        _cur, ver = self.read_state(scope, owner=owner)
        if ver != expected_version:
            raise RuntimeError(f"CAS mismatch: expected {expected_version}, was {ver}")
        new_version = expected_version + 1
        key = (self.session_id, owner or self.owner, scope)
        self.states[key] = (new_version, dict(state) if state else {})
        self.row_counts["state"] = self.row_counts.get("state", 0) + 1
        return new_version

    def seed_state(
        self,
        payload: Mapping[str, object] | None,
        *,
        version: int = 1,
        scope: str = "session",
        owner: str | None = None,
    ) -> None:
        key = (self.session_id, owner or self.owner, scope)
        self.states[key] = (version, dict(payload) if payload else {})

    # --- Event staging ---

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        self.staged_events.append((event_type, dict(payload)))
        self.row_counts[event_type] = self.row_counts.get(event_type, 0) + 1

    # --- Continuations ---

    def stage_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        self.staged_continuations.append((dict(payload), idempotency_key, role))
        self.row_counts["continuation"] = self.row_counts.get("continuation", 0) + 1
        return f"continuation-{len(self.staged_continuations)}"

    enqueue_continuation = stage_continuation

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        self.cancelled_continuations.append(continuation_id)

    # --- Facts and History Reading ---

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        source = self.facts if self.facts else self.events
        remaining = tuple(fact for fact in source if getattr(fact, "event_sequence", getattr(fact, "sequence", 0)) > after_sequence)
        page = remaining[:limit]
        next_seq = getattr(page[-1], "event_sequence", getattr(page[-1], "sequence", after_sequence)) if page else after_sequence
        return FactPage(page, next_seq, len(remaining) > limit)

    def read_context_history(self, *, limit: int = 600) -> ContextHistory:
        events_slice = self.events[:limit]
        records = context_records(
            tuple(
                StoredEvent(
                    self.session_id,
                    e.sequence,  # type: ignore[attr-defined]
                    getattr(e, "run_id", "run-1"),
                    e.event_type,  # type: ignore[attr-defined]
                    getattr(e, "source", "test"),
                    getattr(e, "actor", "user"),
                    getattr(e, "revision_id", "rev-1"),
                    e.payload,  # type: ignore[attr-defined]
                )
                for e in events_slice
            )
        )
        return ContextHistory(records, len(self.events) <= limit)

    def read_transcript(self, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        _ = limit
        return TranscriptPage(entries=(), next_sequence=after_sequence, has_more=False)

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        _ = include_payload
        return tuple(
            e  # type: ignore[misc]
            for e in self.events
            if getattr(e, "sequence", 0) > after_sequence
        )[:limit]

    def session_info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            status="active",
            run_id="run-1",
            revision_id="rev-1",
            lineage=(),
            approval_policy="lenient",
            action_budget_remaining=100,
        )

    # --- Child Sessions ---

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        idempotency_key: str | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        call_number = len(self.spawn_calls) + 1
        self.spawn_calls.append(
            {
                "prompt": prompt,
                "label": label,
                "tool_allowlist": tool_allowlist,
                "max_steps": max_steps,
                "readonly": readonly,
                "notify_on_completion": notify_on_completion,
                "idempotency_key": idempotency_key,
                "result_schema": result_schema,
            }
        )
        refusal = self.spawn_failures.get(call_number)
        if refusal is not None:
            raise refusal if isinstance(refusal, Exception) else ValueError(refusal)
        child_id = f"child-{len(self.roster) + 1}"
        if self.join_output is not None:
            output = self.join_output
        elif label == "review" and self.join_status == "completed":
            output = "review findings: 3 issues"
        elif self.join_status == "completed":
            output = f"result of {prompt}"
        else:
            output = None
        self.roster[child_id] = ChildOutcome(
            child_session_id=child_id,
            status=self.join_status,
            output=output,
            error=self.join_error,
            run_id=f"run-{child_id}",
        )
        return child_id

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        self.send_calls.append(
            {
                "child_session_id": child_session_id,
                "prompt": prompt,
                "label": label,
                "notify_on_completion": notify_on_completion,
            }
        )
        return ChildSendReceipt(
            child_session_id=child_session_id,
            delivery=self.send_result.delivery,
            child_status=self.send_result.child_status,
            run_id=self.send_result.run_id,
            message_id=self.send_result.message_id,
        )

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        self.join_calls.append((child_session_id, timeout_seconds))
        if child_session_id in self.roster:
            return self.roster[child_session_id]
        return ChildOutcome(
            child_session_id=child_session_id,
            status=self.join_status,
            output=self.join_output,
            error=self.join_error,
            run_id=f"run-{child_session_id}",
        )

    def wait_children(self, child_session_ids: Sequence[str]) -> tuple[ChildOutcome, ...]:
        selected = tuple(child_session_ids)
        self.wait_calls.append(selected)
        return tuple(
            self.roster[child_id] for child_id in selected if child_id in self.roster and self.roster[child_id].status not in ("running", "waiting")
        )

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        return tuple(
            DescendantNode(
                session_id=child_id,
                parent_session_id=self.session_id,
                depth=1,
                status=outcome.status,
                run_id=outcome.run_id,
                stop_cause=outcome.stop_cause,
            )
            for child_id, outcome in self.roster.items()
        )

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        self.acks.append((child_session_id, run_id))

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        self.cancel_calls.append((child_session_id, reason))
        return self.cancel_result

    def read_child_history(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> HistoryPage:
        from voidcode.core.history_renderer import render_history_page

        events = [e for e in self.child_events.get(child_session_id, ()) if e.sequence > after_sequence][:limit]
        return render_history_page(events, after_sequence=after_sequence, offset=offset)

    def read_session_history(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> HistoryPage:
        from voidcode.core.history_renderer import render_history_page

        pool = self.events if session_id == self.session_id else self.child_events.get(session_id, ())
        events = [e for e in pool if e.sequence > after_sequence][:limit]
        return render_history_page(events, after_sequence=after_sequence, offset=offset)

    # --- Window Projections ---

    def project_window(self, *, covered_from_sequence: int, covered_to_sequence: int, replacement: str) -> None:
        if self.refuse:
            raise ValueError("core refused the projection")
        covered = tuple(e for e in self.events if getattr(e, "sequence", 0) <= covered_to_sequence)
        covered_bytes = sum(_event_size(e) for e in covered)
        replacement_bytes = len(replacement.encode("utf-8"))
        if covered:
            assert replacement_bytes < covered_bytes, "the fake core measures what the real one does"
        self.projections.append(
            {
                "covered_from_sequence": covered_from_sequence,
                "covered_to_sequence": covered_to_sequence,
                "replacement": replacement,
            }
        )
        self.row = {
            "owner": self.owner,
            "covered_from_sequence": covered_from_sequence,
            "covered_to_sequence": covered_to_sequence,
            "covered_bytes": covered_bytes,
            "dropped_bytes": covered_bytes - replacement_bytes,
            "replacement": replacement,
            "covered_events": covered_to_sequence,
        }

    def window_projection(self) -> Mapping[str, object] | None:
        return self.row

    # --- Overlays ---

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        self.overlay = tier

    def clear_overlay(self) -> bool:
        had = self.overlay is not None
        self.overlay = None
        return had

    # --- Wakeups ---

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        if idempotency_key is None:
            wakeup_id = f"wakeup-{len(self.wakeups)}"
        else:
            wakeup_id = hashlib.sha256("\0".join((self.session_id, self.owner, "wakeup", idempotency_key)).encode()).hexdigest()
        self.wakeups.setdefault(
            wakeup_id,
            {
                "payload": dict(payload),
                "due_at_ms": due_at_ms,
                "role": role,
                "idempotency_key": idempotency_key,
                "status": "pending",
            },
        )
        return wakeup_id

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        record = self.wakeups.get(wakeup_id)
        if record is None or record["status"] != "pending":
            return False
        record["status"] = "cancelled"
        return True

    def pending(self) -> dict[str, dict[str, object]]:
        return {key: value for key, value in self.wakeups.items() if value["status"] == "pending"}

    # --- Queries ---

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        _ = arguments
        return self.queries_dict.get(name)

    def read_resource(self, request: ResourceReadRequest) -> ResourcePage | None:
        if request.scheme in self.resources_dict:
            return self.resources_dict[request.scheme]
        raise ResourceUnavailable(f"no admitted plugin reads the resource scheme {request.scheme!r}")

    ask_query = query

    # --- Tools ---

    def tool_definition(self, name: str) -> ToolDefinition | None:
        if name in self.unavailable_tools:
            return None
        return self.tool_definitions.get(name, ToolDefinition(name=name, description="Test capability"))

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        _ = arguments, expected_revision
        return ToolInvocationReceipt(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tool_name=name,
            depth=1,
        )

    # --- Model Query / Ask ---

    def ask(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> str:
        _ = model, max_output_tokens, include_context
        self.prompts.append(prompt)
        if self.model_error is not None:
            raise self.model_error
        if self.model_answer is not None:
            return self.model_answer
        if self.answer is not None:
            return self.answer
        return ""

    model_query = ask

    def model_query_structured(
        self,
        prompt: str,
        *,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> StructuredModelResult:
        _ = result_schema, model, max_output_tokens, include_context
        self.prompts.append(prompt)
        if self.model_error is not None:
            raise self.model_error
        if self.structured_answer is not None:
            return self.structured_answer

        raw = self.model_answer if self.model_answer is not None else (self.answer if self.answer is not None else "")
        stripped = raw.strip()
        if not stripped:
            return StructuredModelResult(
                raw_text=raw,
                parse_error=StructuredQueryParseError(kind="invalid_json", message="model reply is empty"),
            )
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return StructuredModelResult(
                raw_text=raw,
                parse_error=StructuredQueryParseError(kind="invalid_json", message=f"model reply is not JSON: {exc.msg}"),
            )
        if not isinstance(parsed, dict):
            return StructuredModelResult(
                raw_text=raw,
                parse_error=StructuredQueryParseError(kind="schema_violation", message="top-level JSON value must be an object"),
            )
        owned = own_json_object(parsed)
        try:
            jsonschema.Draft202012Validator(json_wire_object(own_json_object(result_schema))).validate(json_wire_object(owned))
        except jsonschema.ValidationError as exc:
            return StructuredModelResult(
                raw_text=raw,
                parse_error=StructuredQueryParseError(kind="schema_violation", message=exc.message, path=exc.json_path),
            )
        return StructuredModelResult(raw_text=raw, data=owned)

    # --- Usage and Budget ---

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        return SessionUsageReport(
            session_id=self.session_id,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            uncached_input_tokens=0,
            has_unknown_usage=False,
            turn_count=0,
            elapsed_seconds=0.0,
            since_sequence=since_sequence,
            until_sequence=until_sequence,
            through_sequence=self.through_sequence,
        )

    def get_budget(self) -> PluginBudget | None:
        return self.budget

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        self.budget = PluginBudget(
            owner=self.owner,
            since_sequence=since_sequence,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
            max_elapsed_seconds=max_elapsed_seconds,
        )

    def clear_budget(self) -> None:
        self.budget = None

    # --- Artifacts ---

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        sha = hashlib.sha256(raw).hexdigest()
        self.artifacts[sha] = raw
        return ArtifactReference(sha256=sha, bytes=len(raw), media_type=media_type)

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        return self.spill_artifact(b"".join(chunks), media_type=media_type)

    def read_artifact(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        data = self.artifacts[sha256]
        page = data[offset : offset + limit]
        return ArtifactPage(
            artifact=ArtifactReference(sha256, len(data), "text/plain; charset=utf-8"),
            offset=offset,
            data=page,
            eof=offset + len(page) >= len(data),
        )

    # --- Processes ---

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        _ = argv, cwd, env, stdin, timeout_seconds, max_output_bytes, on_output
        return self.process_result or ProcessResult(exit_code=0, stdout=b"", stderr=b"")

    def run_query_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        return self.run_process(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            on_output=on_output,
        )

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        _ = argv, cwd, env
        pid = process_id or f"proc-{uuid.uuid4().hex[:8]}"
        handle = ProcessHandle(process_id=pid, pid=1234, start_time_ms=0, stdout_path="/dev/null", stderr_path="/dev/null")
        self.processes[pid] = handle
        return handle

    def spawn_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bool = False,
        stdout_path: Path | str | None = None,
        stderr_path: Path | str | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        _ = argv, cwd, env, stdin
        pid = process_id or f"proc-{uuid.uuid4().hex[:8]}"
        handle = ProcessHandle(
            process_id=pid,
            pid=1234,
            start_time_ms=0,
            stdout_path=stdout_path or "/dev/null",
            stderr_path=stderr_path or "/dev/null",
        )
        self.processes[pid] = handle
        return handle

    def write_process(self, process_id: str, data: bytes | str) -> int:
        _ = process_id
        return len(data)

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        _ = stream, offset, limit
        return ProcessOutput(process_id=process_id, stream=stream, offset=offset, data=b"", eof=True, running=False)

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        _ = reason
        self.processes.pop(process_id, None)
        return True

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        return tuple(self.processes.values())


@pytest.fixture
def fake_services() -> FakeServices:
    return FakeServices()
