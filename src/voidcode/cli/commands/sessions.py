"""``voidcode sessions``: list, replay, answer, bundle, and revert persisted sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import click

from ...cli_support import (
    EXIT_INVALID_RESOURCE,
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    print_json,
    serialize_event,
    serialize_session_state,
    serialize_stored_session_summary,
)
from ...runtime.bundle import (
    SessionBundleError,
    SessionBundleFormat,
    SessionBundleOptions,
    write_session_bundle,
)
from ...runtime.contracts import NoPendingQuestionError
from ...runtime.permission import PermissionResolution
from ...runtime.question import QuestionResponse
from ...runtime.serialization import serialize_revert_marker, serialize_session_debug_snapshot
from ...runtime.session import StoredSessionSummary
from ..errors import CliError
from ..handler_args import SessionsArgs
from ..options import APPROVAL_DECISIONS, BUNDLE_FORMATS, json_option, show_thinking_option, workspace_option
from ..output import emit_output
from ..presentation import print_runtime_response
from ..runtime_gateway import consume_session_stream, open_runtime, session_result_exit_code


def parse_question_responses(
    *,
    response: tuple[str, ...] = (),
    response_json: str | None = None,
) -> tuple[QuestionResponse, ...]:
    if response_json is not None and response:
        raise CliError(
            code=EXIT_USAGE_ERROR,
            message="--response and --response-json cannot be used together",
        )
    if response_json is not None:
        raw_payload = json.loads(response_json)
        if not isinstance(raw_payload, list) or not raw_payload:
            raise ValueError("--response-json must be a non-empty JSON array")
        raw_items = raw_payload
        parsed: list[QuestionResponse] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise ValueError(f"--response-json[{index}] must be an object")
            item = raw_item
            raw_header = item.get("header")
            if not isinstance(raw_header, str) or not raw_header.strip():
                raise ValueError(f"--response-json[{index}].header must be a non-empty string")
            raw_answers = item.get("answers")
            if not isinstance(raw_answers, list) or not raw_answers:
                raise ValueError(f"--response-json[{index}].answers must be a non-empty array")
            answers: list[str] = []
            for answer_index, raw_answer in enumerate(raw_answers):
                if not isinstance(raw_answer, str) or not raw_answer.strip():
                    raise ValueError(f"--response-json[{index}].answers[{answer_index}] must be a non-empty string")
                answers.append(raw_answer)
            parsed.append(QuestionResponse(header=raw_header, answers=tuple(answers)))
        return tuple(parsed)
    if not response:
        raise CliError(
            code=EXIT_USAGE_ERROR,
            message="at least one --response or --response-json must be provided",
        )
    return (QuestionResponse(header="response", answers=tuple(response)),)


def _handle_sessions_list_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    with open_runtime(workspace) as runtime:
        sessions = list(runtime.list_sessions())
    if not args.include_children:
        sessions = [summary for summary in sessions if summary.session.parent_id is None]

    def _print_sessions() -> None:
        for session in sessions:
            print(_format_session_summary(session))

    return emit_output(
        args,
        {
            "workspace": str(workspace),
            "scope": "all" if args.include_children else "main",
            "sessions": [serialize_stored_session_summary(session) for session in sessions],
        },
        _print_sessions,
    )


def _format_session_summary(session: StoredSessionSummary) -> str:
    return f"SESSION id={session.session.id} status={session.status} turn={session.turn} updated_at={session.updated_at} prompt={session.prompt!r}"


def _handle_sessions_resume_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    if args.dry_run:
        with open_runtime(workspace) as runtime:
            try:
                snapshot = runtime.session_debug_snapshot(session_id=session_id)
            except ValueError as exc:
                raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
        print_json({"workspace": str(workspace), "session_id": session_id, "dry_run": True, "debug": serialize_session_debug_snapshot(snapshot)})
        return EXIT_SUCCESS
    approval_decision: PermissionResolution | None = cast(PermissionResolution | None, args.approval_decision)
    with open_runtime(workspace) as runtime:
        try:
            result = consume_session_stream(
                runtime.resume_stream(
                    session_id,
                    approval_request_id=args.approval_request_id,
                    approval_decision=approval_decision,
                ),
                fallback=lambda: runtime.resume(
                    session_id,
                    approval_request_id=args.approval_request_id,
                    approval_decision=approval_decision,
                ),
                show_thinking=args.show_thinking,
                on_interrupt=lambda interrupted_session_id, run_id: runtime.cancel_session(
                    interrupted_session_id,
                    run_id=run_id,
                    reason="sessions resume KeyboardInterrupt",
                ),
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_runtime_response(result, show_thinking=args.show_thinking)
    return session_result_exit_code(result)


def _handle_sessions_answer_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    question_request_id = args.question_request_id
    assert question_request_id is not None
    try:
        responses = parse_question_responses(response=args.response, response_json=args.response_json)
    except CliError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise CliError(code=EXIT_USAGE_ERROR, message=str(exc)) from None
    with open_runtime(workspace) as runtime:
        try:
            result = consume_session_stream(
                runtime.answer_question_stream(
                    session_id,
                    question_request_id=question_request_id,
                    responses=responses,
                ),
                fallback=lambda: runtime.answer_question(
                    session_id,
                    question_request_id=question_request_id,
                    responses=responses,
                ),
                show_thinking=args.show_thinking,
                on_interrupt=lambda interrupted_session_id, run_id: runtime.cancel_session(
                    interrupted_session_id,
                    run_id=run_id,
                    reason="sessions answer KeyboardInterrupt",
                ),
            )
        except NoPendingQuestionError as exc:
            raise CliError(code=EXIT_INVALID_RESOURCE, message=str(exc)) from None
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    payload = {
        "workspace": str(workspace),
        "session": serialize_session_state(result.session),
        "events": [serialize_event(event, show_thinking=args.show_thinking) for event in result.events],
        "output": result.output,
    }
    if args.json:
        print_json(payload)
    else:
        print_runtime_response(result, show_thinking=args.show_thinking)
    return session_result_exit_code(result)


def _session_bundle_options_from_args(args: SessionsArgs) -> SessionBundleOptions:
    if args.support:
        return SessionBundleOptions.support_artifact()
    return SessionBundleOptions(
        redact=args.redact,
        include_tool_output=args.include_tool_output,
        include_raw_provider_messages=args.include_raw_provider_messages,
        include_reasoning_text=args.include_reasoning_text,
    )


def _handle_sessions_export_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    output_path = args.output
    fmt = args.format
    options = _session_bundle_options_from_args(args)
    with open_runtime(workspace) as runtime:
        try:
            bundle = runtime.export_session_bundle(session_id=session_id, options=options)
        except (ValueError, SessionBundleError) as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    if output_path is None and fmt == "json":
        print(json.dumps(bundle.to_payload(), sort_keys=True))
        return 0

    if output_path is None:
        output_path = Path(f"{session_id}.vcsession.zip")
    try:
        written = write_session_bundle(bundle, path=output_path, fmt=cast(SessionBundleFormat | None, fmt))
    except OSError as exc:
        raise CliError(code=EXIT_RUNTIME_ERROR, message=f"cannot write session bundle {output_path}: {exc}") from None
    print_json(
        {
            "workspace": str(workspace),
            "session_id": session_id,
            "output": str(written),
            "format": fmt,
            "schema": bundle.to_payload()["schema"],
            "manifest": bundle.to_payload()["manifest"],
        }
    )
    return 0


def _handle_sessions_import_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    bundle_path = args.bundle_path
    assert bundle_path is not None
    dry_run = args.dry_run
    with open_runtime(workspace) as runtime:
        try:
            result = runtime.import_session_bundle_file(
                bundle_path=bundle_path,
                dry_run=dry_run,
            )
        except OSError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=f"cannot read session bundle {bundle_path}: {exc}") from None
        except (ValueError, SessionBundleError) as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"workspace": str(workspace), "import": result.to_payload()})
    return EXIT_SUCCESS


def _handle_sessions_debug_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    show_thinking = args.show_thinking
    with open_runtime(workspace) as runtime:
        try:
            snapshot = runtime.session_debug_snapshot(session_id=session_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None

    debug_payload = serialize_session_debug_snapshot(
        snapshot,
        show_thinking=show_thinking,
    )
    print(json.dumps(debug_payload, sort_keys=True))
    return 0


def _handle_sessions_undo_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    with open_runtime(workspace) as runtime:
        try:
            marker = runtime.undo_session(session_id=session_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"session_id": session_id, "revert_marker": serialize_revert_marker(marker)})
    return 0


def _handle_sessions_revert_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    sequence = args.sequence
    assert sequence is not None
    with open_runtime(workspace) as runtime:
        try:
            marker = runtime.revert_session(
                session_id=session_id,
                sequence=sequence,
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"session_id": session_id, "revert_marker": serialize_revert_marker(marker)})
    return 0


def _handle_sessions_unrevert_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    with open_runtime(workspace) as runtime:
        try:
            marker = runtime.unrevert_session(session_id=session_id)
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"session_id": session_id, "revert_marker": serialize_revert_marker(marker)})
    return 0


@click.group(help="Inspect persisted local sessions.")
def sessions() -> None:
    pass


@sessions.command(name="list", help="List persisted main sessions (use --include-children for delegated children).")
@workspace_option("Workspace root used to resolve the local session database.")
@click.option("--include-children", is_flag=True, help="Include delegated child sessions in the listing.")
@json_option("Output persisted sessions as JSON.")
def sessions_list(workspace: Path, include_children: bool, json_output: bool) -> int:
    return _handle_sessions_list_command(SessionsArgs(workspace=workspace, include_children=include_children, json=json_output))


@sessions.command(help="Replay a persisted session response.")
@click.argument("session_id")
@workspace_option("Workspace root used to resolve the local session database.")
@click.option(
    "--approval-request-id",
    help="Optional pending approval request identifier to resolve during resume.",
)
@click.option(
    "--approval-decision",
    type=click.Choice(APPROVAL_DECISIONS),
    help="Optional approval decision applied to the pending request during resume.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Inspect the persisted session without resuming execution.",
)
@show_thinking_option("Show persisted reasoning/thinking events during replay; hidden by default.")
def resume(
    session_id: str,
    workspace: Path,
    approval_request_id: str | None,
    approval_decision: str | None,
    dry_run: bool,
    show_thinking: bool,
) -> int:
    if (approval_request_id is None) != (approval_decision is None):
        raise CliError(
            code=EXIT_USAGE_ERROR,
            message="--approval-request-id and --approval-decision must be provided together",
        )
    return _handle_sessions_resume_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
            dry_run=dry_run,
            show_thinking=show_thinking,
        )
    )


@sessions.command(help="Answer a pending runtime.question_requested session.")
@click.argument("session_id")
@workspace_option("Workspace root used to resolve the local session database.")
@click.option(
    "--question-request-id",
    required=True,
    help="Pending question request identifier to answer.",
)
@click.option(
    "--response",
    multiple=True,
    help="Text answer. Repeat for multi-answer simple responses.",
)
@click.option(
    "--response-json",
    help="JSON array of {header, answers} objects for multi-question answers.",
)
@json_option("Output the resumed runtime response as JSON.")
@show_thinking_option("Show persisted reasoning/thinking events during replay; hidden by default.")
def answer(
    session_id: str,
    workspace: Path,
    question_request_id: str,
    response: tuple[str, ...],
    response_json: str | None,
    json_output: bool,
    show_thinking: bool,
) -> int:
    return _handle_sessions_answer_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            question_request_id=question_request_id,
            response=response,
            response_json=response_json,
            json=json_output,
            show_thinking=show_thinking,
        )
    )


@sessions.command(name="export", help="Export a portable redacted session bundle.")
@click.argument("session_id")
@workspace_option("Workspace root used to resolve the local session database.")
@click.option("--output", type=click.Path(path_type=Path), help="Bundle output path.")
@click.option("--format", "fmt", type=click.Choice(BUNDLE_FORMATS), default="zip")
@click.option("--redact/--no-redact", default=True)
@click.option("--include-tool-output", is_flag=True)
@click.option("--include-raw-provider-messages", is_flag=True)
@click.option("--include-reasoning-text", is_flag=True)
@click.option("--support", is_flag=True)
def sessions_export(
    session_id: str,
    workspace: Path,
    output: Path | None,
    fmt: str,
    redact: bool,
    include_tool_output: bool,
    include_raw_provider_messages: bool,
    include_reasoning_text: bool,
    support: bool,
) -> int:
    return _handle_sessions_export_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            output=output,
            format=fmt,
            redact=redact,
            include_tool_output=include_tool_output,
            include_raw_provider_messages=include_raw_provider_messages,
            include_reasoning_text=include_reasoning_text,
            support=support,
        )
    )


@sessions.command(name="import", help="Import a portable session bundle for local inspection.")
@click.argument("bundle_path", type=click.Path(path_type=Path))
@workspace_option("Workspace root used to resolve the local session database.")
@click.option("--dry-run", is_flag=True)
def sessions_import(bundle_path: Path, workspace: Path, dry_run: bool) -> int:
    return _handle_sessions_import_command(
        SessionsArgs(
            bundle_path=bundle_path,
            workspace=workspace,
            dry_run=dry_run,
        )
    )


@sessions.command(help="Show a minimal runtime-owned debug snapshot for one session.")
@click.argument("session_id")
@workspace_option("Workspace root used to resolve the local session database.")
@show_thinking_option("Include reasoning/thinking text in debug event payloads; hidden by default.")
def debug(session_id: str, workspace: Path, show_thinking: bool) -> int:
    return _handle_sessions_debug_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            json=True,
            show_thinking=show_thinking,
        )
    )


@sessions.command(help="Revert the latest user turn out of provider-facing context.")
@click.argument("session_id")
@workspace_option("Workspace root used to resolve the local session database.")
def undo(session_id: str, workspace: Path) -> int:
    return _handle_sessions_undo_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
        )
    )


@sessions.command(help="Revert provider-facing context to an event sequence.")
@click.argument("session_id")
@click.option("--to", "sequence", type=int, required=True)
@workspace_option("Workspace root used to resolve the local session database.")
def revert(session_id: str, sequence: int, workspace: Path) -> int:
    return _handle_sessions_revert_command(
        SessionsArgs(
            session_id=session_id,
            sequence=sequence,
            workspace=workspace,
        )
    )


@sessions.command(help="Clear an active conversation revert marker.")
@click.argument("session_id")
@workspace_option("Workspace root used to resolve the local session database.")
def unrevert(session_id: str, workspace: Path) -> int:
    return _handle_sessions_unrevert_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
        )
    )
