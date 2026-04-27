from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol, cast

from . import __version__
from .doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    CapabilityDoctor,
    DoctorCheckType,
    create_doctor_for_config,
    create_report,
    format_report,
    format_report_json,
)
from .provider.snapshot import resolved_provider_snapshot
from .runtime.config import (
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeConfig,
    load_runtime_config,
    serialize_provider_fallback_config,
)
from .runtime.config_schema import (
    apply_config_migrations,
    detect_config_migrations,
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    read_runtime_config_payload,
    runtime_config_json_schema,
    write_runtime_config_payload,
)
from .runtime.contracts import (
    BackgroundTaskResult,
    ProviderInspectResult,
    ProviderModelMetadata,
    RuntimeRequest,
    RuntimeSessionDebugSnapshot,
    RuntimeStreamChunk,
    validate_runtime_request_metadata,
)
from .runtime.events import EventEnvelope
from .runtime.permission import PermissionDecision, PermissionResolution
from .runtime.service import VoidCodeRuntime
from .runtime.session import SessionState, StoredSessionSummary
from .runtime.task import BackgroundTaskState, StoredBackgroundTaskSummary
from .server import serve, web

Handler = Callable[[argparse.Namespace], int]


def _format_event(event_type: str, source: str, data: dict[str, object]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in sorted(data.items()))
    if suffix:
        return f"EVENT {event_type} source={source} {suffix}"
    return f"EVENT {event_type} source={source}"


def _close_runtime(runtime: object) -> None:
    exit_method = getattr(runtime, "__exit__", None)
    if callable(exit_method):
        exit_method(None, None, None)


def _handle_run_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    request_text = cast(str, args.request)
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
    )
    runtime = VoidCodeRuntime(workspace=workspace, config=config)
    try:
        metadata: dict[str, object] = {}
        if getattr(args, "skills", None):
            metadata["skills"] = cast(list[str], args.skills)
        if getattr(args, "max_steps", None) is not None:
            metadata["max_steps"] = cast(int, args.max_steps)
        if getattr(args, "provider_stream", None) is not None:
            metadata["provider_stream"] = cast(bool, args.provider_stream)
        request = RuntimeRequest(
            prompt=request_text,
            session_id=cast(str | None, args.session_id),
            metadata=validate_runtime_request_metadata(metadata),
        )
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
        try:
            output = _run_with_inline_approval(
                runtime,
                request,
                interactive=interactive,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None

        if not interactive:
            _print_runtime_output(output)
    finally:
        _close_runtime(runtime)
    return 0


def _run_with_inline_approval(
    runtime: VoidCodeRuntime,
    request: RuntimeRequest,
    *,
    interactive: bool,
) -> str | None:
    output, final_session, last_event = _consume_runtime_stream(runtime.run_stream(request))

    while interactive:
        approval_event = _pending_approval_event(final_session, last_event)
        if approval_event is None:
            break
        output, final_session, last_event = _consume_runtime_stream(
            runtime.resume_stream(
                session_id=final_session.session.id,
                approval_request_id=_approval_request_id(approval_event),
                approval_decision=_prompt_for_approval(approval_event),
            )
        )

    if interactive:
        _print_runtime_output(output)

    return output


def _consume_runtime_stream(
    chunks: Iterator[RuntimeStreamChunk],
) -> tuple[str | None, SessionState, EventEnvelope | None]:
    output: str | None = None
    final_session: SessionState | None = None
    last_event = cast(EventEnvelope | None, None)

    for chunk in chunks:
        final_session = chunk.session
        if chunk.event is not None:
            print(
                _format_event(chunk.event.event_type, chunk.event.source, chunk.event.payload),
                flush=True,
            )
            last_event = chunk.event
        if chunk.kind == "output":
            output = chunk.output

    if final_session is None:
        raise ValueError("runtime stream emitted no chunks")

    return output, final_session, last_event


def _pending_approval_event(
    session: SessionState,
    event: EventEnvelope | None,
) -> EventEnvelope | None:
    if session.status != "waiting":
        return None
    if event is None or event.event_type != "runtime.approval_requested":
        return None
    return event


def _approval_request_id(event: EventEnvelope) -> str:
    return str(event.payload["request_id"])


def _prompt_for_approval(event: EventEnvelope) -> PermissionResolution:
    tool = str(event.payload["tool"])
    target_summary = event.payload.get("target_summary")
    if isinstance(target_summary, str) and target_summary:
        prompt = f"Approve {tool} for {target_summary}? [y/N]: "
    else:
        prompt = f"Approve {tool}? [y/N]: "
    sys.stderr.write(prompt)
    sys.stderr.flush()
    response = sys.stdin.readline()
    normalized = response.strip().lower()
    if normalized in {"y", "yes"}:
        return "allow"
    return "deny"


def _print_runtime_response(
    result: object,
    *,
    event_offset: int = 0,
    include_result: bool = True,
) -> int:
    typed_result = cast("RuntimeResponseLike", result)

    for event in typed_result.events[event_offset:]:
        print(_format_event(event.event_type, event.source, event.payload), flush=True)

    if include_result:
        _print_runtime_output(typed_result.output)
    return len(typed_result.events)


def _print_runtime_output(output: str | None) -> None:
    print("RESULT", flush=True)
    print(output or "", end="", flush=True)
    if output and not output.endswith("\n"):
        print(flush=True)


def _handle_sessions_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        sessions = runtime.list_sessions()
    finally:
        _close_runtime(runtime)

    for session in sessions:
        print(_format_session_summary(session))

    return 0


def _format_session_summary(session: StoredSessionSummary) -> str:
    return (
        f"SESSION id={session.session.id} status={session.status} "
        f"turn={session.turn} updated_at={session.updated_at} prompt={session.prompt!r}"
    )


def _format_named_record(prefix: str, fields: Sequence[tuple[str, object]]) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in fields)
    return f"{prefix} {suffix}" if suffix else prefix


def _background_task_fields(task: BackgroundTaskState) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = [
        ("id", task.task.id),
        ("status", task.status),
        ("parent_session_id", task.parent_session_id),
        ("requested_child_session_id", task.request.session_id),
        ("child_session_id", task.child_session_id),
        ("approval_request_id", task.approval_request_id),
        ("question_request_id", task.question_request_id),
        ("result_available", task.result_available),
    ]
    if task.cancellation_cause is not None:
        fields.append(("cancellation_cause", task.cancellation_cause))
    if task.error is not None:
        fields.append(("error", task.error))
    routing = task.routing_identity
    if routing is not None:
        fields.append(("delegation_mode", routing.mode))
        if routing.category is not None:
            fields.append(("category", routing.category))
        if routing.subagent_type is not None:
            fields.append(("subagent_type", routing.subagent_type))
        if routing.description is not None:
            fields.append(("description", routing.description))
        if routing.command is not None:
            fields.append(("command", routing.command))
    return fields


def _format_background_task_state(task: BackgroundTaskState) -> str:
    return _format_named_record("TASK", _background_task_fields(task))


def _background_task_result_fields(result: BackgroundTaskResult) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = [
        ("id", result.task_id),
        ("status", result.status),
        ("parent_session_id", result.parent_session_id),
        ("requested_child_session_id", result.requested_child_session_id),
        ("child_session_id", result.child_session_id),
        ("approval_request_id", result.approval_request_id),
        ("question_request_id", result.question_request_id),
        ("approval_blocked", result.approval_blocked),
        ("result_available", result.result_available),
    ]
    if result.summary_output is not None:
        fields.append(("summary_output", repr(result.summary_output)))
    if result.error is not None:
        fields.append(("error", result.error))
    routing = result.routing
    if routing is not None:
        fields.append(("delegation_mode", routing.mode))
        if routing.category is not None:
            fields.append(("category", routing.category))
        if routing.subagent_type is not None:
            fields.append(("subagent_type", routing.subagent_type))
        if routing.description is not None:
            fields.append(("description", routing.description))
        if routing.command is not None:
            fields.append(("command", routing.command))
    return fields


def _format_background_task_result(result: BackgroundTaskResult) -> str:
    return _format_named_record("TASK", _background_task_result_fields(result))


def _format_background_task_summary(task: StoredBackgroundTaskSummary) -> str:
    return _format_named_record(
        "TASK",
        [
            ("id", task.task.id),
            ("status", task.status),
            ("session_id", task.session_id),
            ("created_at", task.created_at),
            ("updated_at", task.updated_at),
            ("prompt", repr(task.prompt)),
        ],
    )


def _serialize_session_debug_snapshot(snapshot: RuntimeSessionDebugSnapshot) -> dict[str, object]:
    session_payload: dict[str, object] = {"id": snapshot.session.session.id}
    if snapshot.session.session.parent_id is not None:
        session_payload["parent_id"] = snapshot.session.session.parent_id
    return {
        "session": {
            "session": session_payload,
            "status": snapshot.session.status,
            "turn": snapshot.session.turn,
            "metadata": snapshot.session.metadata,
        },
        "prompt": snapshot.prompt,
        "persisted_status": snapshot.persisted_status,
        "current_status": snapshot.current_status,
        "active": snapshot.active,
        "resumable": snapshot.resumable,
        "replayable": snapshot.replayable,
        "terminal": snapshot.terminal,
        "resume_checkpoint_kind": snapshot.resume_checkpoint_kind,
        "pending_approval": (
            {
                "request_id": snapshot.pending_approval.request_id,
                "tool_name": snapshot.pending_approval.tool_name,
                "target_summary": snapshot.pending_approval.target_summary,
                "reason": snapshot.pending_approval.reason,
                "policy_mode": snapshot.pending_approval.policy_mode,
                "arguments": snapshot.pending_approval.arguments,
                "owner_session_id": snapshot.pending_approval.owner_session_id,
                "owner_parent_session_id": snapshot.pending_approval.owner_parent_session_id,
                "delegated_task_id": snapshot.pending_approval.delegated_task_id,
            }
            if snapshot.pending_approval is not None
            else None
        ),
        "pending_question": (
            {
                "request_id": snapshot.pending_question.request_id,
                "tool_name": snapshot.pending_question.tool_name,
                "question_count": snapshot.pending_question.question_count,
                "headers": list(snapshot.pending_question.headers),
            }
            if snapshot.pending_question is not None
            else None
        ),
        "last_event_sequence": snapshot.last_event_sequence,
        "last_relevant_event": (
            {
                "sequence": snapshot.last_relevant_event.sequence,
                "event_type": snapshot.last_relevant_event.event_type,
                "source": snapshot.last_relevant_event.source,
                "payload": snapshot.last_relevant_event.payload,
            }
            if snapshot.last_relevant_event is not None
            else None
        ),
        "last_failure_event": (
            {
                "sequence": snapshot.last_failure_event.sequence,
                "event_type": snapshot.last_failure_event.event_type,
                "source": snapshot.last_failure_event.source,
                "payload": snapshot.last_failure_event.payload,
            }
            if snapshot.last_failure_event is not None
            else None
        ),
        "failure": (
            {
                "classification": snapshot.failure.classification,
                "message": snapshot.failure.message,
            }
            if snapshot.failure is not None
            else None
        ),
        "last_tool": (
            {
                "tool_name": snapshot.last_tool.tool_name,
                "status": snapshot.last_tool.status,
                "summary": snapshot.last_tool.summary,
                "arguments": snapshot.last_tool.arguments,
                "sequence": snapshot.last_tool.sequence,
            }
            if snapshot.last_tool is not None
            else None
        ),
        "suggested_operator_action": snapshot.suggested_operator_action,
        "operator_guidance": snapshot.operator_guidance,
    }


def _handle_sessions_resume_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    approval_decision = cast(PermissionResolution | None, getattr(args, "approval_decision", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            result = runtime.resume(
                session_id,
                approval_request_id=cast(str | None, getattr(args, "approval_request_id", None)),
                approval_decision=approval_decision,
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    _print_runtime_response(result)
    return 0


def _handle_sessions_debug_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    session_id = cast(str, args.session_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            snapshot = runtime.session_debug_snapshot(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(json.dumps(_serialize_session_debug_snapshot(snapshot), sort_keys=True))
    return 0


def _handle_tasks_status_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    task_id = cast(str, args.task_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            task = runtime.load_background_task(task_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(_format_background_task_state(task))
    return 0


def _handle_tasks_output_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    task_id = cast(str, args.task_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    task_result: BackgroundTaskResult | None = None
    session_output: str | None = None
    try:
        try:
            task_result = runtime.load_background_task_result(task_id)
            if task_result.result_available and task_result.child_session_id is not None:
                try:
                    session_output = runtime.session_result(
                        session_id=task_result.child_session_id
                    ).output
                except ValueError:
                    session_output = None
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    assert task_result is not None
    print(_format_background_task_result(task_result))
    fallback_output = (
        task_result.summary_output if task_result.summary_output is not None else task_result.error
    )
    _print_runtime_output(session_output if session_output is not None else fallback_output)
    return 0


def _handle_tasks_cancel_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    task_id = cast(str, args.task_id)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            task = runtime.cancel_background_task(task_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(_format_background_task_state(task))
    return 0


def _handle_tasks_list_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    parent_session_id = cast(str | None, getattr(args, "parent_session_id", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            tasks = (
                runtime.list_background_tasks_by_parent_session(parent_session_id=parent_session_id)
                if parent_session_id is not None
                else runtime.list_background_tasks()
            )
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    for task in tasks:
        print(_format_background_task_summary(task))
    return 0


def _handle_server_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    config = load_runtime_config(
        workspace,
        approval_mode=cast(PermissionDecision | None, getattr(args, "approval_mode", None)),
    )
    server_entry = cast(Callable[..., None], args.server_entry)
    if hasattr(args, "open_browser"):
        server_entry(
            workspace=workspace,
            host=cast(str, args.host),
            port=cast(int, args.port),
            config=config,
            open_browser=cast(bool, args.open_browser),
        )
    else:
        server_entry(
            workspace=workspace,
            host=cast(str, args.host),
            port=cast(int, args.port),
            config=config,
        )

    return 0


def _handle_config_show_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    session_id = cast(str | None, getattr(args, "session_id", None))
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            effective_config = runtime.effective_runtime_config(session_id=session_id)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "session_id": session_id,
                "approval_mode": effective_config.approval_mode,
                "model": effective_config.model,
                "execution_engine": effective_config.execution_engine,
                "max_steps": effective_config.max_steps,
                "provider_fallback": serialize_provider_fallback_config(
                    getattr(effective_config, "provider_fallback", None)
                ),
                "resolved_provider": resolved_provider_snapshot(
                    getattr(effective_config, "resolved_provider", None)
                ),
            }
        )
    )
    return 0


def _handle_config_schema_command(args: argparse.Namespace) -> int:
    _ = args
    print(json.dumps(runtime_config_json_schema(), indent=2, sort_keys=True))
    return 0


def _handle_config_init_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    try:
        payload = generate_starter_runtime_config(
            approval_mode=cast(str, args.approval_mode),
            execution_engine=(
                cast(str | None, getattr(args, "execution_engine", None)) or "provider"
            ),
            max_steps=cast(int | None, getattr(args, "max_steps", None)),
            include_examples=cast(bool, args.with_examples),
        )
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    if cast(bool, args.print):
        print(format_starter_runtime_config_json(payload), end="")
        return 0

    config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
    if config_path.exists() and not cast(bool, args.force):
        raise SystemExit(
            f"error: runtime config already exists: {config_path}; pass --force to overwrite"
        )
    written_path = write_runtime_config_payload(workspace, payload)
    print(json.dumps({"workspace": str(workspace), "config_path": str(written_path)}))
    return 0


def _handle_config_migrate_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"error: workspace does not exist: {workspace}")

    try:
        payload = read_runtime_config_payload(workspace)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None

    config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
    if payload is None:
        print(
            json.dumps(
                {
                    "workspace": str(workspace),
                    "config_path": str(config_path),
                    "dry_run": not cast(bool, args.write),
                    "migrations": [],
                    "updated_config": None,
                }
            )
        )
        return 0

    migrations = detect_config_migrations(payload)
    updated_payload = apply_config_migrations(payload, migrations)
    should_write = cast(bool, args.write)
    if should_write and migrations:
        write_runtime_config_payload(workspace, updated_payload)

    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "config_path": str(config_path),
                "dry_run": not should_write,
                "migrations": [migration.to_dict() for migration in migrations],
                "updated_config": updated_payload if migrations else None,
            },
            sort_keys=True,
        )
    )
    return 0


def _handle_provider_models_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    provider = cast(str, args.provider)
    refresh = cast(bool, args.refresh)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            if refresh:
                _ = runtime.refresh_provider_models(provider)
            result = runtime.provider_models_result(provider)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    payload: dict[str, object] = {
        "workspace": str(workspace),
        "provider": provider,
        "refreshed": refresh,
        "models": list(result.models),
        "model_metadata": {
            model: _provider_model_metadata_payload(metadata)
            for model, metadata in result.model_metadata.items()
        },
        "source": result.source,
        "last_refresh_status": result.last_refresh_status,
        "last_error": result.last_error,
        "discovery_mode": result.discovery_mode,
    }
    if refresh and result.source == "fallback":
        print(
            "WARN provider.models.refresh "
            f"provider={provider} source=fallback reason={result.last_error}",
            file=sys.stderr,
            flush=True,
        )

    print(json.dumps(payload))
    return 0


def _provider_model_metadata_payload(
    metadata: ProviderModelMetadata,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "context_window": metadata.context_window,
            "max_input_tokens": metadata.max_input_tokens,
            "max_output_tokens": metadata.max_output_tokens,
            "supports_tools": metadata.supports_tools,
            "supports_vision": metadata.supports_vision,
            "supports_streaming": metadata.supports_streaming,
            "supports_reasoning": metadata.supports_reasoning,
            "supports_json_mode": metadata.supports_json_mode,
        }.items()
        if value is not None
    }


def _provider_inspect_payload(
    result: ProviderInspectResult, *, workspace: Path
) -> dict[str, object]:
    return {
        "workspace": str(workspace),
        "provider": {
            "name": result.summary.name,
            "label": result.summary.label,
            "configured": result.summary.configured,
            "current": result.summary.current,
        },
        "models": {
            "provider": result.models.provider,
            "configured": result.models.configured,
            "models": list(result.models.models),
            "model_metadata": {
                model: _provider_model_metadata_payload(metadata)
                for model, metadata in result.models.model_metadata.items()
            },
            "source": result.models.source,
            "last_refresh_status": result.models.last_refresh_status,
            "last_error": result.models.last_error,
            "discovery_mode": result.models.discovery_mode,
        },
        "validation": {
            "provider": result.validation.provider,
            "configured": result.validation.configured,
            "ok": result.validation.ok,
            "status": result.validation.status,
            "message": result.validation.message,
            "source": result.validation.source,
            "last_error": result.validation.last_error,
            "discovery_mode": result.validation.discovery_mode,
        },
        "current_model": result.current_model,
        "current_model_metadata": (
            None
            if result.current_model_metadata is None
            else _provider_model_metadata_payload(result.current_model_metadata)
        ),
    }


def _handle_provider_inspect_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    provider = cast(str, args.provider)
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        try:
            result = runtime.inspect_provider(provider)
        except ValueError as exc:
            raise SystemExit(f"error: {exc}") from None
    finally:
        _close_runtime(runtime)

    print(json.dumps(_provider_inspect_payload(result, workspace=workspace), sort_keys=True))
    return 0


class EventLikeProtocol(Protocol):
    event_type: str
    source: str
    payload: dict[str, object]


class RuntimeResponseLike(Protocol):
    events: tuple[EventLikeProtocol, ...]
    output: str | None

    session: SessionState


def _handle_tui_command(args: argparse.Namespace) -> int:
    workspace = cast(Path, args.workspace)
    approval_mode = cast(PermissionDecision | None, getattr(args, "approval_mode", None))

    from .tui import VoidCodeTUI

    app = VoidCodeTUI(workspace=workspace, approval_mode=approval_mode)
    app.run()
    return 0


def _handle_doctor_command(args: argparse.Namespace) -> int:
    """Run the capability doctor to check external tool readiness."""
    workspace = cast(Path, args.workspace)
    verbose = cast(bool, args.verbose)
    json_output = cast(bool, args.json)

    # Load runtime config to get all capability settings
    config_error: str | None = None
    config: RuntimeConfig | None = None
    results: list[CapabilityCheckResult] = []
    try:
        config = load_runtime_config(workspace)
    except ValueError as exc:
        # Config file has a parse/validation error - report it but continue
        # with minimal checks so the user can still see what's wrong.
        config_error = str(exc)
        doctor = CapabilityDoctor(workspace=workspace)
        doctor.add_executable_check("ast-grep", "ast-grep")
        results = doctor.results
        results.append(
            CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR,
                name="runtime.config",
                check_type=DoctorCheckType.RUNTIME_CONFIG.value,
                error_message=config_error,
            )
        )
    except Exception:
        # OSError (permissions, path not found) and other unexpected errors
        # should propagate so they are not silently swallowed.
        raise

    if config_error is not None:
        print(f"WARN runtime config error: {config_error}", file=sys.stderr, flush=True)

    if config is not None:
        # Create doctor with full config
        doctor = create_doctor_for_config(workspace, config)
        results = doctor.run_all_checks()

    # Create and format report
    report = create_report(results, workspace=workspace)

    if json_output:
        print(format_report_json(report))
    else:
        print(format_report(report, verbose=verbose))

    # Return 0 only when healthy and runtime config parsed successfully.
    return 0 if (report.is_healthy and config_error is None) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voidcode",
        description="Voidcode command-line interface.",
    )
    _ = parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    tui_parser = subparsers.add_parser(
        "tui",
        help="Run the VoidCode interactive Textual UI.",
    )
    _ = tui_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve relative read paths.",
    )
    _ = tui_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this invocation.",
    )
    tui_parser.set_defaults(handler=_handle_tui_command)

    run_parser = subparsers.add_parser(
        "run",
        help=(
            "Run through the local runtime; provider-backed execution is the product path, "
            "and deterministic is an explicit test/dev harness."
        ),
    )
    _ = run_parser.add_argument(
        "request",
        help="Runtime request such as 'read README.md'.",
    )
    _ = run_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve relative read paths.",
    )
    _ = run_parser.add_argument(
        "--session-id",
        help="Optional session identifier used for persisted runs.",
    )
    _ = run_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this invocation.",
    )
    _ = run_parser.add_argument(
        "--skills",
        nargs="+",
        help="Optional skill names applied for this run.",
    )
    _ = run_parser.add_argument(
        "--max-steps",
        type=int,
        help="Optional max graph steps override for this run.",
    )
    stream_group = run_parser.add_mutually_exclusive_group()
    _ = stream_group.add_argument(
        "--provider-stream",
        dest="provider_stream",
        action="store_true",
        help="Enable provider-level streaming for this run.",
    )
    _ = stream_group.add_argument(
        "--no-provider-stream",
        dest="provider_stream",
        action="store_false",
        help="Disable provider-level streaming for this run.",
    )
    run_parser.set_defaults(provider_stream=None)
    run_parser.set_defaults(handler=_handle_run_command)

    serve_parser = subparsers.add_parser(
        "serve",
        help="Serve the local HTTP runtime transport.",
    )
    _ = serve_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used by the local runtime and session database.",
    )
    _ = serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface for the local transport server.",
    )
    _ = serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local transport server.",
    )
    _ = serve_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this server process.",
    )
    serve_parser.set_defaults(handler=_handle_server_command, server_entry=serve)

    web_parser = subparsers.add_parser(
        "web",
        help="Start the local web launcher entrypoint for the runtime transport.",
    )
    _ = web_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used by the local runtime and session database.",
    )
    _ = web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface for the local launcher server.",
    )
    _ = web_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local launcher server.",
    )
    _ = web_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        help="Override the runtime approval mode for this launcher process.",
    )
    _ = web_parser.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Start the web launcher without opening a browser window.",
    )
    web_parser.set_defaults(open_browser=True)
    web_parser.set_defaults(handler=_handle_server_command, server_entry=web)

    sessions_parser = subparsers.add_parser("sessions", help="Inspect persisted local sessions.")
    sessions_subparsers = sessions_parser.add_subparsers(dest="sessions_command")

    tasks_parser = subparsers.add_parser("tasks", help="Inspect delegated background tasks.")
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command")

    config_parser = subparsers.add_parser("config", help="Inspect effective runtime configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    provider_parser = subparsers.add_parser("provider", help="Inspect provider metadata.")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command")

    config_show_parser = config_subparsers.add_parser(
        "show", help="Show effective runtime config for a workspace or session."
    )
    _ = config_show_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config and sessions.",
    )
    _ = config_show_parser.add_argument(
        "--session",
        dest="session_id",
        help="Optional persisted session identifier used to show resumed effective config.",
    )
    config_show_parser.set_defaults(handler=_handle_config_show_command)

    config_schema_parser = config_subparsers.add_parser(
        "schema", help="Print the JSON Schema for .voidcode.json."
    )
    config_schema_parser.set_defaults(handler=_handle_config_schema_command)

    config_init_parser = config_subparsers.add_parser(
        "init", help="Generate a starter workspace .voidcode.json."
    )
    _ = config_init_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root where .voidcode.json should be generated.",
    )
    _ = config_init_parser.add_argument(
        "--approval-mode",
        choices=("allow", "deny", "ask"),
        default="ask",
        help="Starter approval mode to write.",
    )
    _ = config_init_parser.add_argument(
        "--execution-engine",
        choices=("deterministic", "provider"),
        help="Optional execution engine to include in the generated config.",
    )
    _ = config_init_parser.add_argument(
        "--max-steps",
        type=int,
        help="Optional max step budget to include in the generated config.",
    )
    _ = config_init_parser.add_argument(
        "--with-examples",
        action="store_true",
        help="Include minimal tools and skills example blocks.",
    )
    _ = config_init_parser.add_argument(
        "--print",
        action="store_true",
        help="Print the generated config instead of writing it.",
    )
    _ = config_init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .voidcode.json.",
    )
    config_init_parser.set_defaults(handler=_handle_config_init_command)

    config_migrate_parser = config_subparsers.add_parser(
        "migrate", help="Detect and optionally apply .voidcode.json migrations."
    )
    _ = config_migrate_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root containing .voidcode.json.",
    )
    _ = config_migrate_parser.add_argument(
        "--write",
        action="store_true",
        help="Write migrated config back to .voidcode.json. Defaults to dry-run.",
    )
    config_migrate_parser.set_defaults(handler=_handle_config_migrate_command)

    provider_models_parser = provider_subparsers.add_parser(
        "models", help="Show or refresh available models for one provider."
    )
    _ = provider_models_parser.add_argument(
        "provider", help="Provider name, e.g. openai or litellm."
    )
    _ = provider_models_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config.",
    )
    _ = provider_models_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh model list from provider endpoint before printing.",
    )
    provider_models_parser.set_defaults(handler=_handle_provider_models_command)

    provider_inspect_parser = provider_subparsers.add_parser(
        "inspect", help="Show configured status, model limits, and model capabilities."
    )
    _ = provider_inspect_parser.add_argument(
        "provider", help="Provider name, e.g. openai or litellm."
    )
    _ = provider_inspect_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config.",
    )
    provider_inspect_parser.set_defaults(handler=_handle_provider_inspect_command)

    list_parser = sessions_subparsers.add_parser("list", help="List persisted sessions.")
    _ = list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    list_parser.set_defaults(handler=_handle_sessions_list_command)

    resume_parser = sessions_subparsers.add_parser(
        "resume", help="Replay a persisted session response."
    )
    _ = resume_parser.add_argument("session_id", help="Persisted session identifier to load.")
    _ = resume_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = resume_parser.add_argument(
        "--approval-request-id",
        help="Optional pending approval request identifier to resolve during resume.",
    )
    _ = resume_parser.add_argument(
        "--approval-decision",
        choices=("allow", "deny"),
        help="Optional approval decision applied to the pending request during resume.",
    )
    resume_parser.set_defaults(handler=_handle_sessions_resume_command)

    debug_parser = sessions_subparsers.add_parser(
        "debug", help="Show a minimal runtime-owned debug snapshot for one session."
    )
    _ = debug_parser.add_argument("session_id", help="Persisted session identifier to inspect.")
    _ = debug_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = debug_parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output JSON debug snapshot (default).",
    )
    debug_parser.set_defaults(handler=_handle_sessions_debug_command)

    tasks_status_parser = tasks_subparsers.add_parser(
        "status", help="Show delegated task lifecycle state."
    )
    _ = tasks_status_parser.add_argument("task_id", help="Delegated background task identifier.")
    _ = tasks_status_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    tasks_status_parser.set_defaults(handler=_handle_tasks_status_command)

    tasks_output_parser = tasks_subparsers.add_parser(
        "output", help="Show delegated task output and correlation details."
    )
    _ = tasks_output_parser.add_argument("task_id", help="Delegated background task identifier.")
    _ = tasks_output_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    tasks_output_parser.set_defaults(handler=_handle_tasks_output_command)

    tasks_cancel_parser = tasks_subparsers.add_parser(
        "cancel", help="Cancel delegated background work."
    )
    _ = tasks_cancel_parser.add_argument("task_id", help="Delegated background task identifier.")
    _ = tasks_cancel_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    tasks_cancel_parser.set_defaults(handler=_handle_tasks_cancel_command)

    tasks_list_parser = tasks_subparsers.add_parser("list", help="List delegated background tasks.")
    _ = tasks_list_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve the local session database.",
    )
    _ = tasks_list_parser.add_argument(
        "--parent-session",
        dest="parent_session_id",
        help="Optional parent session identifier used to filter delegated tasks.",
    )
    tasks_list_parser.set_defaults(handler=_handle_tasks_list_command)

    # Capability doctor command
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check runtime capability readiness (external tools, formatters, LSP, MCP).",
    )
    _ = doctor_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root used to resolve runtime config.",
    )
    _ = doctor_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all capabilities including successful ones.",
    )
    _ = doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Output report in JSON format.",
    )
    doctor_parser.set_defaults(handler=_handle_doctor_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        getattr(args, "command", None) == "sessions"
        and getattr(args, "sessions_command", None) == "resume"
    ):
        has_request_id = getattr(args, "approval_request_id", None) is not None
        has_decision = getattr(args, "approval_decision", None) is not None
        if has_request_id != has_decision:
            parser.error("--approval-request-id and --approval-decision must be provided together")
    handler = cast(Handler | None, getattr(args, "handler", None))
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
