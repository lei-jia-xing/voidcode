"""``voidcode sessions``: list, replay, answer, bundle, and revert persisted sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, TypedDict, cast

import click
from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, ValidationError

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
    SessionBundleFormat,
    SessionBundleOptions,
    write_session_bundle,
)
from ...runtime.contracts import NoPendingQuestionError
from ...runtime.permission import PermissionResolution
from ...runtime.question import QuestionResponse
from ...runtime.serialization import serialize_revert_marker, serialize_session_debug_snapshot
from ...runtime.session import StoredSessionLineageEntry, StoredSessionSummary
from ..errors import CliError
from ..handler_args import SessionsArgs
from ..options import APPROVAL_DECISIONS, BUNDLE_FORMATS, json_option, show_thinking_option, workspace_option
from ..output import emit_output
from ..presentation import print_runtime_response
from ..runtime_gateway import consume_session_stream, open_runtime, runtime_error_boundary, session_result_exit_code


def _non_empty_text(value: str) -> str:
    if not value.strip():
        raise ValueError("must be a non-empty string")
    return value


_NonEmptyText = Annotated[str, AfterValidator(_non_empty_text)]


class _QuestionResponsePayload(BaseModel):
    """One ``--response-json`` item: a non-empty header and its non-empty answers."""

    header: _NonEmptyText
    answers: Annotated[list[_NonEmptyText], Field(min_length=1)]


_QuestionResponsePayloads = TypeAdapter(list[_QuestionResponsePayload])


def _question_response_error_message(exc: ValidationError) -> str:
    """Render a validation failure with the ``--response-json`` path the user typed."""
    location = exc.errors()[0]["loc"]
    head = f"--response-json[{location[0]}]"
    if len(location) == 1:
        return f"{head} must be an object"
    if location[1] == "header":
        return f"{head}.header must be a non-empty string"
    if len(location) == 2:
        return f"{head}.answers must be a non-empty array"
    return f"{head}.answers[{location[2]}] must be a non-empty string"


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
        try:
            parsed = _QuestionResponsePayloads.validate_python(raw_payload)
        except ValidationError as exc:
            raise ValueError(_question_response_error_message(exc)) from None
        return tuple(QuestionResponse(header=item.header, answers=tuple(item.answers)) for item in parsed)
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
    # The user-set title is the label when present; a title-less session falls
    # back to the prompt, so the line is never label-less.
    label = f"title={session.title!r}" if session.title is not None else f"prompt={session.prompt!r}"
    return f"SESSION id={session.session.id} status={session.status} turn={session.turn} updated_at={session.updated_at} {label}"


def _handle_sessions_resume_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    if args.dry_run:
        with open_runtime(workspace) as runtime, runtime_error_boundary():
            snapshot = runtime.session_debug_snapshot(session_id=session_id)
        print_json({"workspace": str(workspace), "session_id": session_id, "dry_run": True, "debug": serialize_session_debug_snapshot(snapshot)})
        return EXIT_SUCCESS
    # CLI boundary: click.Choice(APPROVAL_DECISIONS) guarantees a PermissionResolution literal.
    approval_decision: PermissionResolution | None = cast(PermissionResolution | None, args.approval_decision)
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        result = consume_session_stream(
            runtime.resume_stream(
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
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        try:
            result = consume_session_stream(
                runtime.answer_question_stream(
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
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        bundle = runtime.export_session_bundle(session_id=session_id, options=options)

    if output_path is None and fmt == "json":
        print(json.dumps(bundle.to_payload(), sort_keys=True))
        return 0

    if output_path is None:
        output_path = Path(f"{session_id}.vcsession.zip")
    try:
        # CLI boundary: click.Choice(BUNDLE_FORMATS) guarantees a SessionBundleFormat literal.
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
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        try:
            result = runtime.import_session_bundle_file(
                bundle_path=bundle_path,
                dry_run=dry_run,
            )
        except OSError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=f"cannot read session bundle {bundle_path}: {exc}") from None
    print_json({"workspace": str(workspace), "import": result.to_payload()})
    return EXIT_SUCCESS


def _handle_sessions_debug_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    show_thinking = args.show_thinking
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        snapshot = runtime.session_debug_snapshot(session_id=session_id)

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
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        marker = runtime.undo_session(session_id=session_id)
    print_json({"session_id": session_id, "revert_marker": serialize_revert_marker(marker)})
    return 0


def _handle_sessions_revert_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    sequence = args.sequence
    assert sequence is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        marker = runtime.revert_session(
            session_id=session_id,
            sequence=sequence,
        )
    print_json({"session_id": session_id, "revert_marker": serialize_revert_marker(marker)})
    return 0


def _handle_sessions_unrevert_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        marker = runtime.unrevert_session(session_id=session_id)
    print_json({"session_id": session_id, "revert_marker": serialize_revert_marker(marker)})
    return 0


def _handle_sessions_rename_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    title = args.title
    assert title is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        summary = runtime.rename_session(session_id=session_id, title=title)
    return emit_output(
        args,
        {"workspace": str(workspace), "session": serialize_stored_session_summary(summary)},
        lambda: print(_format_session_summary(summary)),
    )


def _handle_sessions_fork_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    assert session_id is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        forked = runtime.fork_session(session_id=session_id, at_sequence=args.at_sequence)
    return emit_output(
        args,
        {"workspace": str(workspace), "session": serialize_stored_session_summary(forked)},
        lambda: print(_format_session_summary(forked)),
    )


class _LineageRow(TypedDict):
    session_id: str
    forked_from_session_id: str | None
    forked_at_sequence: int | None
    depth: int


def _lineage_rows(
    entries: tuple[StoredSessionLineageEntry, ...],
) -> list[_LineageRow]:
    """Project lineage entries, oldest ancestor first, depth-annotated.

    Depth is derived client-side from the ``forked_from_session_id`` edges so
    the caller can indent; the walk itself is a read-only ancestry chain.
    """
    depth_by_id: dict[str, int] = {}
    rows: list[_LineageRow] = []
    for entry in entries:
        parent = entry.forked_from_session_id
        depth = 0 if parent is None else depth_by_id.get(parent, 0) + 1
        depth_by_id[entry.session_id] = depth
        rows.append(
            {
                "session_id": entry.session_id,
                "forked_from_session_id": parent,
                "forked_at_sequence": entry.forked_at_sequence,
                "depth": depth,
            }
        )
    return rows


def _handle_sessions_tree_command(args: SessionsArgs) -> int:
    workspace = args.workspace
    session_id = args.session_id
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        entries = runtime.session_lineage(session_id=session_id)
    rows = _lineage_rows(entries)

    def _print_lineage() -> None:
        for row in rows:
            indent = "  " * int(row["depth"])
            origin = f" <- {row['forked_from_session_id']}@{row['forked_at_sequence']}" if row["forked_from_session_id"] is not None else ""
            print(f"{indent}{row['session_id']}{origin}")

    return emit_output(
        args,
        {
            "workspace": str(workspace),
            "root": session_id,
            "lineage": rows,
        },
        _print_lineage,
    )


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


@sessions.command(help="Set a short display title for a persisted session.")
@click.argument("session_id")
@click.argument("title")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output the renamed session summary as JSON.")
def rename(session_id: str, title: str, workspace: Path, json_output: bool) -> int:
    return _handle_sessions_rename_command(
        SessionsArgs(
            session_id=session_id,
            title=title,
            workspace=workspace,
            json=json_output,
        )
    )


@sessions.command(help="Fork a session: copy its event history into a new, independently continuable session.")
@click.argument("session_id")
@click.option(
    "--at-sequence",
    "at_sequence",
    type=int,
    default=None,
    help="Copy events 1..N. Defaults to the session's latest event.",
)
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output the new session summary as JSON.")
def fork(session_id: str, at_sequence: int | None, workspace: Path, json_output: bool) -> int:
    return _handle_sessions_fork_command(
        SessionsArgs(
            session_id=session_id,
            at_sequence=at_sequence,
            workspace=workspace,
            json=json_output,
        )
    )


@sessions.command(help="Show fork lineage (oldest ancestor first).")
@click.argument("session_id", required=False)
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output the lineage entries as JSON.")
def tree(session_id: str | None, workspace: Path, json_output: bool) -> int:
    return _handle_sessions_tree_command(
        SessionsArgs(
            session_id=session_id,
            workspace=workspace,
            json=json_output,
        )
    )
