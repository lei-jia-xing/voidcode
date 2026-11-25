"""Model-facing rendering of child outcomes, groups, and pending runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from voidcode.sdk.resources import ResourceReadCaller
from voidcode.sdk.services import ArtifactServices, ChildOutcome, ChildStatus, DescendantNode

from .bodies import TaskBody, _BatchChild
from .constants import OUTPUT_PAGE_BYTES
from .output import _output_facts, _OutputFacts, _structured_facts, _StructuredFacts
from .state import ChildRecord, DelegationState
from .uris import _agent_uri, _artifact_uri, _transcript_uri

_KNOWN_CHILD_STATUSES: frozenset[ChildStatus] = frozenset({"idle", "running", "waiting", "interrupted", "completed", "failed"})


def _header(task_id: str, label: str, subagent_type: str | None = None) -> str:
    header = f"task {task_id}"
    if subagent_type:
        header += f" profile={subagent_type}"
    return header + (f' label="{label}"' if label else "")


def _structured_line(structured: _StructuredFacts) -> str | None:
    """One honest structured-delivery line; ``None`` when status governs instead."""
    if structured.data is not None:
        return "structured: ok"
    if structured.parse_error is not None:
        error = structured.parse_error
        location = f" at {error.path}" if error.path is not None else ""
        return f"structured: {error.kind}{location}: {error.message}"
    if structured.unavailable is not None:
        return f"structured: unavailable ({structured.unavailable})"
    return None


def _outcome_text(
    task_id: str,
    label: str,
    outcome: ChildOutcome,
    subagent_type: str | None = None,
    *,
    facts: _OutputFacts | None = None,
    budget_exhaustion: Mapping[str, object] | None = None,
    structured: _StructuredFacts | None = None,
) -> str:
    lines = [f"{_header(task_id, label, subagent_type)} status={outcome.status} run_id={outcome.run_id or 'unknown'}"]
    if outcome.stop_cause:
        lines.append(f"stop_cause: {outcome.stop_cause}")
    if outcome.error:
        lines.append(f"error: {outcome.error}")
    if budget_exhaustion is not None:
        details = ", ".join(f"{key}={value}" for key, value in budget_exhaustion.items())
        lines.append(f"budget_exhaustion: {details}")
    if outcome.output_error:
        lines.append(f"output_error: {outcome.output_error}")
    if outcome.history_error:
        lines.append(f"history_error: {outcome.history_error}")
    if structured is not None:
        structured_line = _structured_line(structured)
        if structured_line is not None:
            lines.append(structured_line)
    if facts is not None and facts.output_uri is not None:
        lines.append(f"Full immutable output for run_id={outcome.run_id or 'unknown'}: {facts.output_uri}.")
    if facts is not None and facts.output:
        if facts.output_offset is not None and facts.output_bytes is not None:
            start = facts.output_offset
            end = start + len(facts.output.encode("utf-8"))
            if start or end < facts.output_bytes:
                lines.append(f"output bytes {start}..{end} of {facts.output_bytes}")
        lines.append(facts.output)
        if facts.next_output_offset is not None:
            if facts.output_uri is None:
                raise RuntimeError("paged child output has no immutable output URI")
            lines.append(f"More output artifact: {facts.output_uri}; continue at byte_offset={facts.next_output_offset}.")
    elif facts is not None and facts.output_preview:
        lines.append(facts.output_preview)
        lines.append("(output preview only; the full output could not be stored as an artifact)")
    lines.append(f"Current status/latest published output: {_agent_uri(task_id)} (distinct from the transcript).")
    lines.append(f'History: {_transcript_uri(task_id)} (page it with task(operation="history", task_id="{task_id}", after_sequence=...)).')
    if outcome.history_sha256 is not None:
        lines.append(f"Saved history snapshot: {_artifact_uri(outcome.history_sha256)} (immutable).")
    if outcome.status == "running":
        lines.append(f'Still running: await it with task(operation="output", task_id="{task_id}").')
    elif outcome.status == "waiting":
        lines.append("The child is parked waiting for approval; resume it before expecting a terminal result.")
    return "\n".join(lines)


def _outcome_entry(
    task_id: str,
    record: ChildRecord,
    outcome: ChildOutcome,
    services: ArtifactServices,
    caller: ResourceReadCaller,
) -> tuple[dict[str, object], str]:
    budget_exhaustion = _budget_exhaustion_payload(outcome)
    facts = _output_facts(outcome, services, caller, 0, OUTPUT_PAGE_BYTES)
    structured = _structured_facts(record.result_schema, outcome, services)
    body = TaskBody(
        operation="output",
        task_id=task_id,
        label=record.label,
        status=outcome.status,
        output=facts.output,
        error=outcome.error,
        stop_cause=outcome.stop_cause,
        budget_exhaustion=budget_exhaustion,
        subagent_type=record.subagent_type,
        run_id=outcome.run_id,
        output_uri=facts.output_uri,
        output_offset=facts.output_offset,
        output_bytes=facts.output_bytes,
        next_output_offset=facts.next_output_offset,
        output_preview=facts.output_preview,
        output_error=outcome.output_error,
        history_error=outcome.history_error,
        data=structured.data,
        parse_error=None if structured.parse_error is None else structured.parse_error.as_payload(),
        structured_unavailable=structured.unavailable,
        agent_uri=_agent_uri(task_id),
        history_uri=_transcript_uri(task_id),
        history_sha256=outcome.history_sha256,
    ).as_payload()
    body.pop("operation")
    return body, _outcome_text(
        task_id,
        record.label,
        outcome,
        record.subagent_type,
        facts=facts,
        budget_exhaustion=budget_exhaustion,
        structured=structured,
    )


def _render_outcomes(
    outcomes: Sequence[ChildOutcome],
    state: DelegationState,
    services: ArtifactServices,
    caller: ResourceReadCaller,
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...]]:
    children: list[dict[str, object]] = []
    rendered: list[str] = []
    seen_runs: set[tuple[str, str]] = set()
    for outcome in outcomes:
        task_id = outcome.child_session_id
        run_id = outcome.run_id
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("delivered child outcome has no exact run identity")
        record = state.children.get(task_id)
        if record is None:
            raise RuntimeError("delivered child outcome is not in this plugin's managed state")
        identity = (task_id, run_id)
        if identity in seen_runs:
            raise RuntimeError("child wait returned the same result more than once")
        seen_runs.add(identity)
        entry, text = _outcome_entry(task_id, record, outcome, services, caller)
        children.append(entry)
        rendered.append(text)
    return tuple(children), tuple(rendered)


def _pending_entries(
    state: DelegationState,
    descendants: Sequence[DescendantNode],
    selected_task_ids: set[str],
) -> tuple[dict[str, object], ...]:
    pending: list[dict[str, object]] = []
    for descendant in descendants:
        task_id = descendant.session_id
        if task_id not in selected_task_ids:
            continue
        record = state.children.get(task_id)
        if record is None:
            continue
        if descendant.status not in _KNOWN_CHILD_STATUSES:
            raise RuntimeError(f"descendant returned an unknown wait status: {descendant.status!r}")
        current_work = descendant.status in ("idle", "running", "waiting") or (
            descendant.status == "interrupted" and descendant.stop_cause not in ("owner_cancel", "user_stop")
        )
        if not current_work and not descendant.has_queued_followup:
            continue
        parked_followup = (
            descendant.has_queued_followup and descendant.status == "interrupted" and descendant.stop_cause not in ("owner_cancel", "user_stop")
        )
        entry: dict[str, object] = {
            "task_id": task_id,
            "label": record.label,
            "status": descendant.status,
            "agent_uri": _agent_uri(task_id),
        }
        if record.subagent_type:
            entry["subagent_type"] = record.subagent_type
        if descendant.run_id:
            entry["run_id"] = descendant.run_id
        if descendant.stop_cause:
            entry["stop_cause"] = descendant.stop_cause
        if descendant.activity:
            entry["activity"] = descendant.activity
        if descendant.has_queued_followup:
            entry["has_queued_followup"] = True
        if parked_followup:
            entry["parked_followup"] = True
        if descendant.awaiting_approval:
            entry["awaiting_approval"] = True
        pending.append(entry)
    return tuple(pending)


def _pending_text(entries: Sequence[Mapping[str, object]]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        line = f"- pending {entry['task_id']} status={entry['status']} run_id={entry.get('run_id') or 'none'}"
        if entry.get("parked_followup") is True:
            line += " (parked follow-up; resume or cancel the child before it can be consumed)"
        lines.append(line)
    return lines


def _budget_exhaustion_payload(outcome: ChildOutcome) -> dict[str, object] | None:
    """Serialize the SDK's typed exhaustion without inferring it from stop_cause."""
    return None if outcome.budget_exhaustion is None else outcome.budget_exhaustion.as_payload()


def _batch_text(group_id: str, created: Sequence[_BatchChild], failed: Sequence[tuple[int, str]]) -> str:
    total = len(created) + len(failed)
    if failed:
        header = f"Started {len(created)} of {total} child task(s) in parallel group {group_id}; {len(failed)} item(s) failed."
    else:
        header = f"Started {len(created)} child task(s) in parallel group {group_id}."
    lines = [header]
    for entry in created:
        lines.append(
            f"- [{entry.index}] {entry.task_id} agent_uri={_agent_uri(entry.task_id)}"
            + (f" profile={entry.subagent_type}" if entry.subagent_type else "")
        )
    lines.extend(f"- [{index}] failed: {message}" for index, message in failed)
    return "\n".join(lines)


def _group_text(group_id: str, entries: Sequence[Mapping[str, object]], pending: Sequence[Mapping[str, object]]) -> str:
    lines = [f"Parallel group {group_id}: {len(entries)} delivered child result(s), {len(pending)} pending child run(s)."]
    lines.extend(f"- {entry['task_id']} status={entry['status']} run_id={entry.get('run_id') or 'unknown'}" for entry in entries)
    lines.extend(_pending_text(pending))
    return "\n".join(lines)
