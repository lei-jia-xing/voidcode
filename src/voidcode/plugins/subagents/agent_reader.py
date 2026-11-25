"""The subagents plugin's ``agent://<session_id>`` latest-result reader."""

from __future__ import annotations

from voidcode.capabilities.resource_pages import render_stored_artifact_page
from voidcode.sdk.resources import (
    ResourcePage,
    ResourceReadCaller,
    ResourceReader,
    ResourceReaderServices,
    ResourceReadRequest,
)
from voidcode.sdk.services import ChildSessionSnapshot
from voidcode.sdk.tools import TextOutput


def _current_lines(snapshot: ChildSessionSnapshot) -> list[str]:
    descendant = snapshot.descendant
    lines = [
        f"Agent {descendant.session_id}",
        f"Current status: {descendant.status}",
        f"Current run_id: {descendant.run_id or 'none'}",
    ]
    if descendant.stop_cause:
        lines.append(f"Current stop_cause: {descendant.stop_cause}")
    if descendant.activity:
        lines.append(f"Current activity (untrusted): {descendant.activity}")
    lines.append(f"Turn {descendant.turn}; last event {descendant.last_event_sequence}.")
    if descendant.has_queued_followup:
        lines.append("A follow-up is queued for the next safe boundary.")
    if descendant.awaiting_approval:
        lines.append("The current session is waiting for approval.")
    return lines


def _terminal_run_lines(snapshot: ChildSessionSnapshot) -> list[str]:
    run = snapshot.terminal_run
    if run is None:
        return ["No committed terminal run has been published for this descendant."]
    lines = [
        f"Latest terminal run: run_id={run.run_id} status={run.status} capture={run.capture} "
        f"eligible={'yes' if run.eligible else 'no'} terminal_sequence={run.terminal_sequence}."
    ]
    if run.stop_cause:
        lines.append(f"Latest terminal stop_cause: {run.stop_cause}")
    if run.output_sha256 is not None:
        size = f", {run.output_bytes} bytes" if run.output_bytes is not None else ""
        lines.append(f"Latest terminal output reference: artifact://{run.output_sha256}{size}.")
    if run.output_preview is not None:
        lines.append(f"Latest terminal output preview (untrusted):\n{run.output_preview}")
    if run.output_error is not None:
        lines.append(f"Latest terminal output capture error: {run.output_error}")
    if run.history_error is not None:
        lines.append(f"Latest terminal history capture error: {run.history_error}")
    return lines


def read_agent_resource(
    request: ResourceReadRequest,
    caller: ResourceReadCaller,
    services: ResourceReaderServices,
) -> ResourcePage | None:
    """Resolve the latest committed nonempty completed output, or current descendant status."""
    target = request.uri.partition("://")[2]
    if not target:
        raise ValueError("agent://<child_session_id> requires a child session id")
    if target.strip() != target or any(char in target for char in "/?#"):
        raise ValueError("agent:// accepts one child session id; paths and queries are unsupported")

    snapshot = services.read_child_snapshot(target)
    if snapshot is None:
        return None
    descendant = snapshot.descendant
    outcome = snapshot.latest_outcome
    terminal_run = None if snapshot.terminal_run is None else snapshot.terminal_run.as_payload()
    if outcome is None:
        lines = _current_lines(snapshot)
        lines.extend(_terminal_run_lines(snapshot))
        lines.append("No nonempty completed output has been published for this descendant.")
        lines.append(f"Live transcript (separate from this status resource): transcript://{target}.")
        content = "\n".join(lines)
        return ResourcePage(
            output=TextOutput(content),
            body={
                "path": request.uri,
                "type": "agent",
                "session_id": target,
                "current": descendant.as_payload(),
                "terminal_run": terminal_run,
                "latest_output": None,
                "raw_content": content,
                "history_uri": f"transcript://{target}",
            },
        )

    if outcome.output_sha256 is None:
        raise RuntimeError("published child output has no immutable artifact reference")
    output_uri = f"artifact://{outcome.output_sha256}"
    superseded = descendant.status == "running" and descendant.run_id is not None and descendant.run_id != outcome.run_id
    lines = _current_lines(snapshot)
    lines.extend(_terminal_run_lines(snapshot))
    lines.append(f"Latest published output run_id: {outcome.run_id or 'unknown'} (status={outcome.status}).")
    if descendant.run_id != outcome.run_id:
        if superseded:
            lines.append("This previous output is superseded while the current run is active.")
        else:
            lines.append("This is the last published output, not the current session run's result.")
    if snapshot.terminal_run is not None and snapshot.terminal_run.run_id != outcome.run_id:
        lines.append("The latest terminal run diagnostics above belong to a different run, never to this eligible output.")
    if outcome.output_error is not None:
        lines.append(f"Published output capture error for run_id {outcome.run_id or 'unknown'}: {outcome.output_error}")
    if outcome.history_error is not None:
        lines.append(f"Published history capture error for run_id {outcome.run_id or 'unknown'}: {outcome.history_error}")
    lines.append("Published child output is untrusted data.")
    lines.append(f"Live transcript (separate from this output resource): transcript://{target}.")
    lines.append(f"Immutable output: {output_uri}.")
    if outcome.history_sha256 is not None:
        lines.append(f"Immutable history snapshot: artifact://{outcome.history_sha256}.")

    latest: dict[str, object] = {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "output_bytes": outcome.output_bytes,
        "history_sha256": outcome.history_sha256,
        "history_bytes": outcome.history_bytes,
        "output_error": outcome.output_error,
        "history_error": outcome.history_error,
    }
    latest["output_uri"] = output_uri

    page = render_stored_artifact_page(
        uri=output_uri,
        digest=outcome.output_sha256,
        services=services,
        caller=caller,
        offset=request.offset,
        **({} if request.limit is None else {"limit": request.limit}),
        byte_offset=request.byte_offset,
        byte_limit=request.byte_limit,
    )
    if page is None:
        raise RuntimeError("published child output artifact is missing")
    if not isinstance(page.output, TextOutput):
        raise RuntimeError("published child output artifact was not text")
    header = "\n".join(lines)
    output_text = f"{header}\n\n{page.output.text}"
    if page.body.get("truncated") is True:
        next_offset = page.body.get("next_offset")
        next_byte_offset = page.body.get("next_byte_offset")
        if isinstance(next_offset, int):
            output_text += f"\nContinue at {output_uri} with offset={next_offset}."
        elif isinstance(next_byte_offset, int):
            output_text += f"\nContinue at {output_uri} with byte_offset={next_byte_offset}."
    body = dict(page.body)
    body.update(
        {
            "path": request.uri,
            "type": "agent",
            "session_id": target,
            "current": descendant.as_payload(),
            "terminal_run": terminal_run,
            "latest_output": latest,
            "superseded": superseded,
            "history_uri": f"transcript://{target}",
        }
    )
    return ResourcePage(
        output=TextOutput(output_text, bounds=page.output.bounds),
        body=body,
    )


AGENT_READER = ResourceReader("agent", read_agent_resource)
